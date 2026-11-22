"""Calling the model.

Every call goes through :meth:`LLMService.generate`, which does the same four things in
the same order: pick a key with budget, send, record what it actually cost, and — when
Google disagrees with our accounting — mark that pair spent and try the next key rather
than retrying into the same wall.

Rotation chooses by remaining daily budget and believes a ``429`` immediately, because a
retry loop that tries each key in turn burns a request from every key on a bad day.
"""

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx2
import structlog
from openai import DefaultAsyncHttpxClient

from ..schemas import camel_dict
from .budget import Ledger
from .failures import classify, failure
from .keys import BudgetExhaustedError, KeyStore, LLMError, LLMUnavailable
from .openai_compat import OVERLOADED_WAITS, OpenAICompatible, ProviderError
from .providers import PROVIDERS
from .text import estimate_tokens

if TYPE_CHECKING:
    from openai.types.shared import ReasoningEffort

    from ..db.sqlite import Database
    from ..sealing import Sealer
    from .registry import ModelInfo, ModelRegistry

log = structlog.get_logger(__name__)

#: How long a single generation may take before we give up on it.
TIMEOUT_SECONDS = 180.0


@dataclass(slots=True)
class Answer:
    """One model response, and the tokens it cost."""

    text: str
    model: str
    prompt_tokens: int
    output_tokens: int
    thinking_tokens: int
    total_tokens: int
    usage_reported: bool = True

    @property
    def usage(self) -> dict[str, int]:
        return camel_dict(self, exclude=("text", "model"))


def _status(exc: Exception) -> int | None:
    return exc.status if isinstance(exc, ProviderError) else None


def _is_rate_limit(exc: Exception) -> tuple[bool, bool]:
    "``(rate limited, daily)``. Only the provider's details say which limit was hit."
    text = str(exc).lower()
    if _out_of_credit(text):
        return False, False
    limited = _status(exc) == 429 or "resource_exhausted" in text or "quota" in text
    daily = "per day" in text or "perday" in text or "daily" in text or "requests per day" in text
    return limited, daily


def _rejects_temperature(exc: Exception) -> bool:
    """A 400 about the temperature: the model takes none but its own default."""
    return _status(exc) == 400 and "temperature" in str(exc).lower()


def _refused(exc: Exception) -> bool:
    "Whether the key was turned away rather than the request: another key may get in."
    text = str(exc).lower()
    return _status(exc) in (401, 403) or "api_key_invalid" in text or _out_of_credit(text)


def _out_of_credit(text: str) -> bool:
    "An account with no credit left: a ``429`` naming a quota, but no wait ends it."
    return "insufficient_quota" in text


def _token_count(value: Any) -> int:
    """Untrusted usage must never turn a request into a token credit."""
    if value is None:
        return 0
    if type(value) is not int or value < 0:
        raise ValueError("Invalid provider token count")
    return value


#: How long a connection to a provider stays open between calls: a fresh one costs about
#: 0.4 s of set-up, measured against Google, on answers that take two or three seconds.
KEEPALIVE_SECONDS = 60.0

#: Models that reject ``minimal``: "not supported for Gemini 3.8 Flash" per Google's docs.
_NO_MINIMAL = ("gemini-3.7", "gemini-3.8")

_EFFORTS: dict[str, ReasoningEffort] = {
    "MINIMAL": "minimal",
    "LOW": "low",
    "MEDIUM": "medium",
    "HIGH": "high",
}


def _effort(model: ModelInfo, thinking: str | None) -> ReasoningEffort:
    """Callers ask for a thinking level; this is the ``reasoning_effort`` the model accepts.

    Only Gemini 3 takes one, and 3.7 and 3.8 refuse ``minimal`` with a 400 on every message.
    """
    if not thinking or model.provider != "google" or not model.id.startswith("gemini-3"):
        return None
    effort = _EFFORTS[thinking.upper()]
    return "low" if effort == "minimal" and model.id.startswith(_NO_MINIMAL) else effort


class LLMService:
    """The assistant: keys, budget and models in one place."""

    def __init__(self, db: Database, sealer: Sealer, registry: ModelRegistry) -> None:
        self.db = db
        self.registry = registry
        self.ledger = Ledger(db)
        self.keys = KeyStore(db, sealer, self.ledger)
        #: Models that refused a temperature. Learned from the refusal, because nothing
        #: publishes which models take one; forgotten on restart, costing one 400 each.
        self._default_sampling: set[str] = set()
        self._http = DefaultAsyncHttpxClient(
            timeout=TIMEOUT_SECONDS, limits=httpx2.Limits(keepalive_expiry=KEEPALIVE_SECONDS)
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- plumbing --------------------------------------------------------

    def _client(self, provider: str, secret: str) -> OpenAICompatible:
        # Not providers.get: its fallback to Google would send another provider's key there.
        spec = PROVIDERS.get(provider)
        if spec is None:
            raise LLMError(f"{provider!r} is not a provider this application knows.")
        return OpenAICompatible(spec.base_url, secret, self._http)

    async def runnable(self) -> list[dict[str, Any]]:
        """Set-up models whose provider has an enabled Key, most requests left today first.

        A model set up without such a Key is listed under LLM Integration but cannot answer,
        so nothing that sends a request offers it.
        """
        status = await self.keys.status(self.registry)
        enabled = {k["provider"] for k in status["keys"] if k["enabled"]}
        found = [
            {
                "id": b["model"],
                "ref": b["ref"],
                "provider": b["provider"],
                "remainingToday": b["remainingToday"],
            }
            for b in status["budget"]
            if b["provider"] in enabled
        ]
        return sorted(found, key=lambda m: -m["remainingToday"])

    def model_for(self, ref: str | None) -> ModelInfo:
        "The model asked for, or with none named, the set-up one with most to spend."
        if not ref:
            models = self.registry.all()
            if not models:
                raise LLMError("No model is set up yet. Set one up under LLM Integration › Models.")
            return models[0]
        model = self.registry.get(ref)
        if model is None:
            raise LLMError(f"{ref} is not set up. Set it up under LLM Integration › Models.")
        return model

    # -- the one call ----------------------------------------------------

    async def generate(
        self,
        *,
        system: str,
        user: str,
        model_ref: str | None,
        response_schema: dict[str, Any],
        temperature: float,
        thinking: str | None = None,
        max_provider_attempts: int | None = None,
        max_output_tokens: int | None = None,
    ) -> Answer:
        """Send one prompt, rotating keys by remaining budget. ``model_ref`` names the model
        as :func:`.registry.model_ref` spells it.

        ``thinking`` is one of Google's thinking levels. Not a free upgrade: thinking tokens
        are billed against the same per-minute budget as the answer.
        """
        model = self.model_for(model_ref)
        estimate = estimate_tokens(system) + estimate_tokens(user)
        tried: set[int] = set()
        refusal: LLMError | None = None
        effort = _effort(model, thinking)
        # Google holds the answer to the schema. The others are only asked for a JSON object,
        # which not all of them honour either; the prompt names the keys it needs.
        schema = response_schema if model.provider == "google" else None
        sampling = None if model.ref in self._default_sampling else temperature
        provider_attempts = 0
        overloaded = 0
        last_failure: LLMError | None = None

        while True:
            # The controlled pilot forbids retrying a physical request through a
            # second key, a rejected temperature, or a 503. The SDK uses zero retries.
            if max_provider_attempts is not None and provider_attempts >= max_provider_attempts:
                if last_failure is not None:
                    raise last_failure from None
                raise LLMUnavailable("Controlled pilot provider request limit reached.")
            try:
                slot = await self.keys.choose(model, skip=tried)
            except BudgetExhaustedError as exc:
                # Every key has been tried: one that was turned away says more than a budget.
                if refusal is not None:
                    raise refusal from None
                if tried:
                    exc.args = (f"{exc.args[0]} Already tried: {len(tried)} key(s).",)
                raise
            key_id = slot.key_id
            tried.add(key_id)

            # The durable request charge is never refunded after a send may start.
            secret = await self.keys.secret(key_id)
            client = self._client(model.provider, secret)

            try:
                provider_attempts += 1
                text, usage = await asyncio.wait_for(
                    client.generate(
                        model=model.id,
                        system=system,
                        user=user,
                        temperature=sampling,
                        schema=schema,
                        reasoning_effort=effort,
                        single_attempt=True,
                        **(
                            {"max_output_tokens": max_output_tokens}
                            if max_output_tokens is not None
                            else {}
                        ),
                    ),
                    timeout=TIMEOUT_SECONDS,
                )
            except TimeoutError as exc:
                await self.keys.mark(key_id, error="Timed out")
                # The provider had the request and may well bill it, so it counts.
                raise LLMUnavailable(
                    f"{model.id} did not answer within {TIMEOUT_SECONDS:.0f} seconds. "
                    "Try a smaller question or a lighter model.",
                    diagnostic=failure("timeout"),
                ) from exc
            except Exception as exc:  # noqa: BLE001 — fail closed without logging secrets
                diagnostic = classify(exc)
                kind = LLMUnavailable if diagnostic.retryable else LLMError
                error = kind(diagnostic.message, diagnostic=diagnostic)
                last_failure = error
                # Persist only allowlisted diagnostics, not provider bodies which can echo
                # prompts or credentials. Campaign events retain the same classification.
                await self.keys.mark(key_id, error=diagnostic.message)
                if sampling is not None and _rejects_temperature(exc):
                    # The key is fine; the model only wants its own default. Same key again.
                    log.info("llm.temperature_refused", model=model.id)
                    self._default_sampling.add(model.ref)
                    sampling = None
                    tried.discard(key_id)
                    continue
                if (
                    _status(exc) == 503
                    and max_provider_attempts is None
                    and overloaded < len(OVERLOADED_WAITS)
                ):
                    wait = OVERLOADED_WAITS[overloaded]
                    overloaded += 1
                    tried.discard(key_id)
                    await asyncio.sleep(wait)
                    continue
                limited, daily = _is_rate_limit(exc)
                if limited:
                    # Google is the authority: mark this pair spent and rotate rather than
                    # retrying into a limit we evidently mis-tracked.
                    await self.ledger.penalise(key_id, model, daily=daily)
                    log.warning("llm.rate_limited", key_id=key_id, model=model.id, daily=daily)
                    continue
                if _refused(exc):
                    log.warning("llm.key_refused", key_id=key_id, model=model.id)
                    refusal = error
                    continue
                raise error from None

            try:
                prompt = _token_count(usage.get("prompt_tokens"))
                output = _token_count(usage.get("completion_tokens"))
                reported = _token_count(usage.get("total_tokens"))
                details = usage.get("completion_tokens_details") or {}
                reasoning = _token_count(details.get("reasoning_tokens"))
                if reported and reported < prompt + output:
                    raise ValueError("Inconsistent provider token totals")
            except ValueError, AttributeError, TypeError:
                diagnostic = failure("malformed_response")
                await self.keys.mark(key_id, error=diagnostic.message)
                raise LLMError(
                    diagnostic.message, diagnostic=diagnostic, observed={"requests": 1}
                ) from None
            # Google reports no breakdown: its thinking is only what the total holds beyond
            # the prompt and the answer.
            thought = reasoning or max(0, reported - prompt - output)
            total = reported or estimate
            if reported or prompt or output or thought:
                await self.ledger.record_tokens(slot, model, reported or prompt + output + thought)
            await self.keys.mark(key_id)

            answer = Answer(
                text=text.strip(),
                model=model.id,
                prompt_tokens=prompt,
                output_tokens=output,
                thinking_tokens=thought,
                total_tokens=total,
                usage_reported=bool(usage) and reported > 0,
            )
            category = usage.get("_response_failure")
            if isinstance(category, str):
                diagnostic = failure(category)
                observed = {"requests": 1}
                if answer.usage_reported:
                    observed.update(tokens=total, input=prompt, output=max(output, total - prompt))
                raise LLMError(diagnostic.message, diagnostic=diagnostic, observed=observed)
            return answer

    # -- health ----------------------------------------------------------

    async def _listed(self, provider: str, key_id: int) -> list[str]:
        secret = await self.keys.secret(key_id)
        # Anthropic pages its list twenty at a time; Google refuses unknown parameters.
        query = {"limit": 1000} if provider == "anthropic" else {}
        return await self._client(provider, secret).models(**query)

    async def check_key(self, key_id: int) -> dict[str, Any]:
        """Confirm a key works, without spending a generation request.

        ``models.list`` is not billed against the generation quotas, so a health check
        costs nothing that matters.
        """
        row = await self.keys.get(key_id)
        provider = str(getattr(row, "provider", "google") or "google")
        try:
            names = await self._listed(provider, key_id)
        # Any provider failure is the key test's answer, stored and shown to the user.
        except Exception as exc:  # noqa: BLE001 — fail closed without logging secrets  # noqa: BLE001
            await self.keys.mark(key_id, error=str(exc)[:300])
            return {"keyId": key_id, "ok": False, "error": str(exc)[:300]}

        await self.keys.mark(key_id)
        return {"keyId": key_id, "ok": True, "models": len(names)}

    async def offered(self, provider: str) -> dict[str, Any]:
        """The model ids a provider's keys can reach, to pick from when setting one up.

        Asked of the first enabled key, live: the list is only a convenience, so a failure
        says why and leaves the user to type the id instead.
        """
        rows = [r for r in await self.keys.list_keys() if r.enabled and r.provider == provider]
        if not rows:
            return {"models": [], "error": "Add a Key for this provider first."}
        try:
            return {"models": await self._listed(provider, rows[0].id), "error": None}
        # The reason is shown beside the field where the id can still be typed.
        except Exception as exc:  # noqa: BLE001 — fail closed without logging secrets  # noqa: BLE001
            return {"models": [], "error": str(exc)[:300]}
