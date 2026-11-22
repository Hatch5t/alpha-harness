"""Allowlisted diagnostics. Never persist provider bodies, prompts or response text here."""

from dataclasses import asdict, dataclass

from openai import APIConnectionError, APIResponseValidationError, APITimeoutError

from .openai_compat import ProviderError

_REASONS = {
    "schema_complexity": (
        "Provider rejected the response schema as too complex.",
        "Update schema compatibility; review remaining global caps before a new campaign.",
        False,
    ),
    "request_schema": (
        "Provider rejected the structured-output request.",
        "Check provider structured-output support and update the application before retrying.",
        False,
    ),
    "authentication": (
        "Provider did not accept the API credentials.",
        "Check the key in LLM Integration. Never paste it into a research report.",
        False,
    ),
    "model_access": (
        "This key cannot access the selected model.",
        "Select an accessible model in a new campaign; check project permissions.",
        False,
    ),
    "quota": (
        "Provider quota or account credit is exhausted.",
        "Check provider quota and billing; wait for reset or select an approved model.",
        False,
    ),
    "rate_limit": (
        "Provider rate limit reached.",
        "Wait for the provider limit to reset before authorizing another attempt.",
        True,
    ),
    "timeout": (
        "Provider did not answer before the timeout.",
        "The request may have been billed. Check provider usage before approving another attempt.",
        True,
    ),
    "connection": (
        "Connection to the provider failed.",
        "Check connectivity; the request may have reached the provider. "
        "Review usage before retrying.",
        True,
    ),
    "provider_unavailable": (
        "Provider is temporarily unavailable.",
        "Wait or select another approved model. A retry consumes another request.",
        True,
    ),
    "http_error": (
        "Provider rejected the HTTP request.",
        "Check model and request compatibility before approving another attempt.",
        False,
    ),
    "malformed_response": (
        "Provider response was not valid JSON.",
        "Use a model supporting JSON output. No candidate was accepted from this response.",
        False,
    ),
    "schema_validation": (
        "JSON response failed local research validation.",
        "Review response-format compatibility; local constraints remain enforced.",
        False,
    ),
    "empty_response": (
        "Provider returned no response content.",
        "Check model output support and output limits before authorizing a new campaign.",
        False,
    ),
    "output_limit": (
        "Provider response hit its output-token limit.",
        "Use a smaller generation batch or review token caps in a new campaign.",
        False,
    ),
    "refusal": (
        "Provider declined to generate this response.",
        "Review the research request before authorizing a new campaign.",
        False,
    ),
    "configuration": (
        "Local model or key configuration prevented generation.",
        "Check LLM Integration and the campaign's frozen model configuration.",
        False,
    ),
    "unknown": (
        "Generation failed; legacy records do not retain the cause.",
        "Unknown usage is not zero. Update the app before approving a new campaign.",
        False,
    ),
}


@dataclass(frozen=True, slots=True)
class Failure:
    category: str
    message: str
    recovery: str
    retryable: bool
    http_status: int | None = None

    def document(self) -> dict[str, object]:
        data = asdict(self)
        data["httpStatus"] = data.pop("http_status")
        return data


def failure(category: str, status: int | None = None) -> Failure:
    message, recovery, retryable = _REASONS[category]
    return Failure(category, message, recovery, retryable, status)


def classify(exc: Exception) -> Failure:
    if isinstance(exc, TimeoutError | APITimeoutError):
        return failure("timeout")
    if isinstance(exc, APIConnectionError):
        return failure("connection")
    if isinstance(exc, APIResponseValidationError | ValueError | TypeError | AttributeError):
        return failure("malformed_response")
    if not isinstance(exc, ProviderError):
        return failure("configuration")
    status, text = exc.status, str(exc).lower()
    if (
        status == 400
        and "schema" in text
        and ("too many states" in text or "too complex" in text or "too much branching" in text)
    ):
        return failure("schema_complexity", status)
    if status == 401 or "api_key_invalid" in text or "unauthenticated" in text:
        return failure("authentication", status)
    if (
        "insufficient_quota" in text
        or "resource_exhausted" in text
        or (status == 429 and ("quota" in text or "daily" in text))
    ):
        return failure("quota", status)
    if status in {403, 404}:
        return failure("model_access", status)
    if status == 429:
        return failure("rate_limit", status)
    if status >= 500:
        return failure("provider_unavailable", status)
    if status == 400 and any(word in text for word in ["schema", "response_format"]):
        return failure("request_schema", status)
    return failure("http_error", status)
