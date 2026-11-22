"""Adapters over the existing catalog, LLM service, queue and optimizer.

Offline records are terminal when created. They can never enter the live engine queue,
and synthetic alphas never enter the user's analytical Vault.
"""

import hashlib
import json
import math
from dataclasses import replace
from datetime import date, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import select

if TYPE_CHECKING:
    from ..brain.schemas import SimulationRequest
    from ..labs.template_document import FieldsVariable
from ..catalog.queries import Tuple4
from ..db.models import DispatchIntent, ResearchProfile, SimStatus, SimulationRecord, utcnow
from ..engine.lifecycle import new_record, request_hash
from ..labs.power_pool import Context, Field, context_for, operators_text
from ..labs.study import Optimizer
from ..llm.text import loads_or
from .schemas import CampaignSpec, Generated, GeneratedAlpha, Hypothesis, Profile

SYSTEM = """Develop testable economic hypotheses before alpha expressions. Use only the supplied
account operators and cached field names. Return the requested JSON schema: hypotheses and
alphas, each alpha referring to a zero-based hypothesis index. Include direction, horizon,
assumptions, risk factors, failure regimes and falsification tests. Do not promise performance.
Explore distinct mechanisms, not cosmetic transformations of one static ranking. VECTOR fields
must go straight into a supported vector reducer before scalar/time operators. Do not use
credentials, external tools or unknown fields. No alpha is submission-ready from a Quick result.
"""

# Provider-facing schema deliberately omits Pydantic's $defs/$ref, titles, defaults,
# and length constraints. These are validated locally by Generated.model_validate;
# the provider is only asked to return the right structural shape. Gemini's
# OpenAI-compatibility endpoint does not uniformly accept every JSON Schema keyword.
_STR = {"type": "string"}
_STRINGS = {"type": "array", "items": {"type": "string"}}
_HYPOTHESIS_PROPERTIES: dict[str, Any] = {
    "mechanism": _STR,
    "family": _STR,
    "direction": _STR,
    "horizon": _STR,
    "assumptions": _STRINGS,
    "fieldsRequired": _STRINGS,
    "riskFactors": _STRINGS,
    "failureRegimes": _STRINGS,
    "falsificationTests": _STRINGS,
    "independence": _STR,
    "estimatedSimulations": {"type": "integer"},
}
GENERATION_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "hypotheses": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": _HYPOTHESIS_PROPERTIES,
                "required": [
                    "mechanism",
                    "family",
                    "direction",
                    "horizon",
                    "assumptions",
                    "fieldsRequired",
                    "riskFactors",
                    "failureRegimes",
                    "falsificationTests",
                    "independence",
                ],
            },
        },
        "alphas": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "expression": _STR,
                    "hypothesis": {"type": "integer"},
                },
                "required": ["expression", "hypothesis"],
            },
        },
    },
    "required": ["hypotheses", "alphas"],
}


SYNTHETIC_OPERATORS = [
    {"name": "rank", "definition": "rank(x)"},
    {"name": "ts_delta", "definition": "ts_delta(x, d)"},
    {"name": "ts_rank", "definition": "ts_rank(x, d)"},
    {"name": "ts_mean", "definition": "ts_mean(x, d)"},
    {"name": "vec_avg", "definition": "vec_avg(x)"},
]


async def no_network(*_: Any, **__: Any) -> Any:
    raise RuntimeError("Synthetic research cannot use a live endpoint.")


class SyntheticEngine:
    def __init__(self, db: Any) -> None:
        self.db = db

    async def enqueue(
        self, requests: list[SimulationRequest], *, task: str, intent_keys: list[str], **_: Any
    ) -> dict[str, Any]:
        if len(requests) != len(intent_keys):
            raise ValueError("Synthetic dispatch cardinality mismatch.")
        outcomes = []
        async with self.db.session() as session:
            for request, key in zip(requests, intent_keys, strict=True):
                digest = request_hash(request)
                intent = await session.get(DispatchIntent, key)
                if intent:
                    if intent.request_hash != digest or intent.record_id is None:
                        raise ValueError("Synthetic dispatch identity changed.")
                    record = await session.get(SimulationRecord, intent.record_id)
                    if record is None:
                        raise ValueError("Original synthetic record is missing.")
                else:
                    alpha_id = "mock_" + hashlib.sha256(key.encode()).hexdigest()[:24]
                    record = new_record(
                        request,
                        task=task,
                        status=SimStatus.COMPLETE,
                        alpha_id=alpha_id,
                        allow_uncertain_retry=False,
                        finished_at=utcnow(),
                        message="Synthetic offline fixture; no BRAIN call.",
                    )
                    session.add(record)
                    await session.flush()
                    session.add(DispatchIntent(key=key, request_hash=digest, record_id=record.id))
                outcomes.append(
                    {
                        "recordId": record.id,
                        "alphaId": record.alpha_id,
                        "status": str(record.status),
                    }
                )
        return {"outcomes": outcomes}

    async def set_quota(self, *_: Any, **__: Any) -> None:
        return None

    async def drop_queued(self, *_: Any, **__: Any) -> int:
        return 0


class SyntheticVault:
    def __init__(self, db: Any) -> None:
        self.db = db

    async def by_ids(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        async with self.db.session() as session:
            records = list(
                await session.scalars(
                    select(SimulationRecord).where(SimulationRecord.alpha_id.in_(ids))
                )
            )
        return {str(r.alpha_id): self.row(r) for r in records}

    @staticmethod
    def row(record: SimulationRecord) -> dict[str, Any]:
        index = int(hashlib.sha256((record.expression or "").encode()).hexdigest()[:6], 16) % 11
        return {
            "alpha_id": record.alpha_id,
            "expression": record.expression,
            "simulation_mode": record.payload["settings"]["simulationMode"],
            "region": record.region,
            "delay": record.delay,
            "status": "UNSUBMITTED",
            "sharpe": 1.5 + index / 50,
            "fitness": 0.8 + index / 50,
            "turnover": 0.01,
            "returns": 0.1,
            "drawdown": 0.05,
            "margin": 0.001,
            "checks": json.dumps(
                [
                    {"name": "LOW_SHARPE", "result": "PASS"},
                    {"name": "LOW_GLB_AMER_SHARPE", "result": "PASS"},
                ]
            ),
        }

    async def with_families(self, rows: dict[str, Any]) -> dict[str, Any]:
        return rows

    async def series(self, ids: list[str]) -> dict[str, dict[date, tuple[float, float]]]:
        stored = await self.by_ids(ids)
        first = date(2018, 1, 1)
        days = (date(2025, 12, 31) - first).days + 1
        result = {}
        for alpha_id in ids:
            if alpha_id not in stored and alpha_id != "synthetic-baseline":
                continue
            expression = str(stored.get(alpha_id, {}).get("expression", ""))
            correlated = "synthetic_liquidity" in expression or alpha_id == "synthetic-baseline"
            result[alpha_id] = {
                first + timedelta(days=i): (
                    12_000 + 90_000 * math.sin(i / (5 if correlated else 13)),
                    float("nan") if i == 100 else 0.01,
                )
                for i in range(days)
            }
        return result

    async def submitted_members(self) -> list[dict[str, Any]]:
        return [
            {
                "alpha_id": "synthetic-baseline",
                "expression": "rank(synthetic_baseline)",
                "status": "ACTIVE",
                "date_submitted": date(2017, 1, 1),
            }
        ]


class ResearchIO:
    def __init__(self, state: Any, *, offline: bool) -> None:
        self.state = state
        self.offline = offline
        self.engine = SyntheticEngine(state.db) if offline else state.engine
        self.vault = SyntheticVault(state.db) if offline else state.alphas
        self.optimizer = (
            Optimizer(
                state.db,
                cast("Any", self.engine),
                cast("Any", SimpleNamespace(get_alpha=no_network)),
                cast("Any", SimpleNamespace()),
                on_change=no_network,
                alphas=cast("Any", self.vault),
            )
            if offline
            else state.optimizer
        )

    async def operators(self) -> list[dict[str, Any]]:
        return (
            SYNTHETIC_OPERATORS
            if self.offline
            else await self.state.metadata.cached_operators() or []
        )

    async def contexts(self, spec: CampaignSpec) -> list[Context]:
        if self.offline:
            datasets = spec.dataset_ids or ["synthetic_analyst", "synthetic_liquidity"]
            result = []
            for dataset in datasets:
                if dataset not in {"synthetic_analyst", "synthetic_liquidity"}:
                    continue
                names = {f"{dataset}_matrix": "MATRIX", f"{dataset}_vector": "VECTOR"}
                result.append(
                    Context(
                        dataset,
                        dataset,
                        "Synthetic fixtures",
                        "Offline only.",
                        tuple(
                            Field(k, v, 1.0, "Synthetic test field", None) for k, v in names.items()
                        ),
                        (),
                        (),
                        names,
                        frozenset(names),
                        frozenset(names),
                        {k: frozenset({spec.universe}) for k in names},
                    )
                )
            return result
        rows = await self.state.queries.datasets(
            Tuple4(region=spec.region, delay=spec.delay, universe=spec.universe)
        )
        ids = (
            spec.dataset_ids
            or [
                str(r["dataset_id"])
                for r in sorted(
                    rows, key=lambda r: (-(float(r.get("coverage") or 0)), str(r["dataset_id"]))
                )
            ][:30]
        )
        profile = spec.profile_evidence
        if profile is None:
            async with self.state.db.session() as session:
                row = await session.get(ResearchProfile, spec.profile_id)
            profile = Profile.model_validate(row.document) if row else None
        contexts = []
        for dataset in ids:
            if (
                profile
                and profile.field_filter
                and profile.field_filter.dataset_ids
                and dataset not in profile.field_filter.dataset_ids
            ):
                continue
            ctx = await context_for(
                self.state.catalog,
                spec.region,
                spec.delay,
                [spec.universe],
                dataset,
                narrow=profile.field_filter if profile else None,
            )
            if ctx and ctx.fields:
                if profile and profile.field_filter:
                    allowed = frozenset(f.id for f in (*ctx.fields, *ctx.basics)) | frozenset(
                        ctx.groups
                    )
                    ctx = replace(
                        ctx,
                        names=allowed,
                        types={k: v for k, v in ctx.types.items() if k in allowed},
                        held={k: v for k, v in ctx.held.items() if k in allowed},
                    )
                contexts.append(ctx)
        return contexts

    async def template_fields(
        self, spec: CampaignSpec, ctx: Context, variable: FieldsVariable
    ) -> tuple[Field, ...]:
        if variable.dataset_ids and ctx.id not in variable.dataset_ids:
            return ()
        if variable.filter is None:
            return ctx.fields
        if self.offline:
            # Fixtures can prove type/coverage filtering, not live catalog eligibility.
            return tuple(
                f
                for f in ctx.fields
                if not variable.filter.field_types or f.type in variable.filter.field_types
            )
        narrowed = await context_for(
            self.state.catalog,
            spec.region,
            spec.delay,
            [spec.universe],
            ctx.id,
            narrow=variable.filter,
        )
        allowed = narrowed.own if narrowed else frozenset()
        return tuple(f for f in ctx.fields if f.id in allowed)

    def prompt(
        self, spec: CampaignSpec, ctx: Context, operators: list[dict[str, Any]], memory: str
    ) -> str:
        fields = [
            {"id": f.id, "type": f.type, "coverage": f.coverage, "description": f.description[:160]}
            for f in ctx.fields[:80]
        ]
        return json.dumps(
            {
                "goal": spec.hypothesis,
                "opportunityRules": spec.profile_evidence.model_dump(mode="json")
                if spec.profile_evidence
                else None,
                "market": f"{spec.region}/D{spec.delay}",
                "dataset": {"id": ctx.id, "category": ctx.category, "description": ctx.description},
                "fields": fields,
                "operators": operators_text(operators),
                "previousFailures": memory,
                "instruction": "Return 3 falsifiable mechanisms and 20 diverse expressions. "
                "An alpha's hypothesis is its zero-based index. Do not repeat failed ideas "
                "without an explicit changed mechanism. No performance guarantees.",
            }
        )

    async def generate(
        self, spec: CampaignSpec, ctx: Context, prompt: str
    ) -> tuple[Generated, dict[str, int]]:
        if not self.offline:
            answer = await self.state.llm.generate(
                system=SYSTEM,
                user=prompt,
                model_ref=spec.model,
                response_schema=GENERATION_RESPONSE_SCHEMA,
                temperature=1.0,
                max_provider_attempts=1,
                max_output_tokens=spec.max_output_tokens,
            )
            return Generated.model_validate(loads_or(answer.text)), (
                {
                    "requests": 1,
                    "tokens": answer.total_tokens,
                    "input": answer.prompt_tokens,
                    "output": max(answer.output_tokens, answer.total_tokens - answer.prompt_tokens),
                }
                if answer.usage_reported
                else {"requests": 1}
            )
        hypotheses = [
            Hypothesis(
                mechanism=f"Synthetic {family} mechanism in {ctx.id}; not a real alpha claim.",
                family=family,
                direction="Positive synthetic association",
                horizon=f"{i + 1} to 20 days",
                assumptions=["Synthetic fixture data only"],
                fields_required=[ctx.fields[0].id],
                risk_factors=["Synthetic exposure"],
                failure_regimes=["Sign reversal"],
                falsification_tests=[
                    "Negative after-cost validation or redundant portfolio correlation"
                ],
                independence="Must be tested against the synthetic baseline.",
            )
            for i, family in enumerate(["revision", "liquidity", "stability"])
        ]
        alphas = [
            GeneratedAlpha(expression=f"rank({op}({ctx.id}_matrix, {i + 1}))", hypothesis=i % 3)
            for i, op in enumerate(["ts_delta", "ts_rank", "ts_mean"] * 6)
        ]
        alphas += [alphas[0], GeneratedAlpha(expression=f"rank({ctx.id}_vector)", hypothesis=0)]
        return Generated(hypotheses=hypotheses, alphas=alphas), {
            "requests": 1,
            "microusd": 0,
            "tokens": 100,
            "input": 40,
            "output": 60,
        }

    async def harvest(self, study_id: int) -> None:
        async with self.optimizer.lock(study_id):
            await self.optimizer.harvest_research(study_id)
