"Durable, approval-gated research controller. No submission transition exists."

import asyncio
import contextlib
import hashlib
import itertools
import json
import math
from collections import Counter
from datetime import timedelta
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select, update

from ..account import SCHEMA_KEY
from ..brain.schemas import FULL_MODE, QUICK_MODE, SimulationRequest, SimulationSettings
from ..brain.settings_schema import validate_settings
from ..db.models import (
    MetadataCache,
    ResearchBudget,
    ResearchCampaign,
    ResearchCandidate,
    ResearchDailyCharge,
    ResearchEvent,
    ResearchExposure,
    ResearchHypothesis,
    ResearchProfile,
    ResearchReservation,
    SimulationRecord,
    Study,
    StudyStatus,
    Trial,
    TrialState,
    utcnow,
)
from ..labs import scheduler, template
from ..labs.fastexpr import operator_table
from ..labs.notebook_import import ImportRefused, preview_notebook
from ..labs.pilot import candidates as quick_choices
from ..labs.power_pool import Context, Rejected, check
from ..labs.power_pool_preflight import repair_temporal_vector
from ..labs.template_document import FieldsVariable
from ..llm.keys import BudgetExhaustedError, LLMError
from . import budget, evidence, profiles
from .io import SYSTEM, ResearchIO
from .schemas import SAMPLER, CampaignSpec, Generated, GeneratedAlpha, Hypothesis, Limits, Profile


class ResearchError(ValueError):
    """Actionable domain refusal, without credentials or provider payloads."""


class ResearchService:
    def __init__(self, state: Any) -> None:
        self.state = state
        self.db = state.db
        self._locks: dict[int, asyncio.Lock] = {}
        self._task: asyncio.Task[None] | None = None
        self._jobs: dict[int, asyncio.Task[None]] = {}
        self._ios = {
            mode: ResearchIO(state, offline=mode == "offline") for mode in ("offline", "live")
        }

    async def initialize(self) -> None:
        await profiles.initialize(self.db)
        async with self.db.session() as session:
            for mode, limits in [
                ("offline", Limits(quick=1000, full=100, requests=100, tokens=100_000_000)),
                ("live", Limits(quick=0, full=0, requests=0, tokens=0, microusd=0)),
            ]:
                if await session.get(ResearchBudget, mode) is None:
                    session.add(ResearchBudget(mode=mode, limits=limits.model_dump(mode="json")))
            uncharged = list(
                await session.scalars(
                    select(ResearchReservation)
                    .outerjoin(
                        ResearchDailyCharge,
                        (ResearchDailyCharge.reservation_id == ResearchReservation.id)
                        & (ResearchDailyCharge.day == ResearchReservation.day),
                    )
                    .where(ResearchDailyCharge.id.is_(None))
                )
            )
            for reservation in uncharged:
                session.add(
                    ResearchDailyCharge(
                        reservation_id=reservation.id,
                        mode=reservation.mode,
                        day=reservation.day,
                        meter=reservation.meter,
                        amount=reservation.amount,
                    )
                )

    async def start_worker(self) -> None:
        await self.initialize()
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="research-controller")

    async def stop_worker(self) -> None:
        for task in [self._task, *self._jobs.values()]:
            if task is not None:
                task.cancel()
        for task in [self._task, *self._jobs.values()]:
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._task = None
        self._jobs.clear()

    async def _run(self) -> None:
        while True:
            await self.tick()
            await asyncio.sleep(2)

    async def tick(self) -> None:
        self._jobs = {key: task for key, task in self._jobs.items() if not task.done()}
        async with self.db.session() as session:
            rows = list(
                await session.scalars(
                    select(ResearchCampaign)
                    .where(
                        ResearchCampaign.status.in_(["RUNNING", "BLOCKED", "PAUSED", "CANCELLED"])
                    )
                    .order_by(ResearchCampaign.updated_at, ResearchCampaign.id)
                )
            )
        for row in rows:
            if row.status != "RUNNING" and not (row.retry_at and row.retry_at <= utcnow()):
                async with self.db.session() as session:
                    open_work = await session.scalar(
                        select(func.count())
                        .select_from(Trial)
                        .where(
                            Trial.study_id == row.study_id,
                            Trial.state.in_([TrialState.QUEUED, TrialState.RUNNING]),
                        )
                    )
                if not open_work:
                    continue
            if len(self._jobs) >= 2:
                break
            if row.id not in self._jobs:
                self._jobs[row.id] = asyncio.create_task(
                    self.advance(row.id), name=f"research-{row.id}"
                )

    async def get(self, campaign_id: int) -> ResearchCampaign:
        async with self.db.session() as session:
            row = await session.get(ResearchCampaign, campaign_id)
            if row is None:
                raise ResearchError("Research campaign was not found.")
            return row

    async def event(
        self, session: Any, campaign_id: int, key: str, kind: str, payload: dict[str, Any]
    ) -> None:
        exists = await session.scalar(
            select(ResearchEvent.id).where(
                ResearchEvent.campaign_id == campaign_id, ResearchEvent.key == key
            )
        )
        if exists is None:
            session.add(ResearchEvent(campaign_id=campaign_id, key=key, kind=kind, payload=payload))

    async def _cursor(self, campaign_id: int, **changes: Any) -> None:
        async with self.db.session() as session:
            row = await session.get(ResearchCampaign, campaign_id)
            if row is not None:
                row.cursor = {**row.cursor, **changes}

    async def preview(self, spec: CampaignSpec) -> dict[str, Any]:
        io = self._ios[spec.mode]
        contexts = await io.contexts(spec)
        operators = await io.operators()
        async with self.db.session() as session:
            profile_row = await session.get(ResearchProfile, spec.profile_id)
            history = list(await session.scalars(select(ResearchHypothesis)))
        profile = spec.profile_evidence or (
            Profile.model_validate(profile_row.document) if profile_row else None
        )
        problems = (
            profiles.problems(profile, spec.region, live=spec.mode == "live")
            if profile
            else ["Research profile is missing."]
        )
        if profile and profile.required_theme != spec.expected_theme and profile.required_theme:
            problems.append("The profile requires its recorded platform theme.")
        if spec.profile_id == "power_pool" and spec.purpose != "power_pool":
            problems.append(
                "Power Pool profile requires Power Pool validation and operator limits."
            )
        if profile and profile.component_regions:
            if not spec.component_group:
                problems.append(
                    "Multi-region research needs a shared componentGroup for "
                    "independent regional campaigns."
                )
            if spec.region not in profile.component_regions:
                problems.append(
                    "The selected region is not one of this profile's required components."
                )
        if not contexts:
            problems.append(
                "No eligible cached fields. Sync the selected market and "
                "datasets; the campaign is preserved."
            )
        if not operators:
            problems.append(
                "No cached account operators. Refresh platform metadata before running."
            )
        if spec.mode == "live":
            async with self.db.session() as session:
                metadata = await session.get(MetadataCache, SCHEMA_KEY)
                active_legacy = await session.scalar(
                    select(func.count())
                    .select_from(Study)
                    .where(Study.sampler != SAMPLER, Study.status == StudyStatus.RUNNING)
                )
            if active_legacy:
                problems.append(
                    "Pause existing lab tasks before authorizing an autonomous campaign."
                )
            if metadata is None:
                problems.append(
                    "No cached account settings schema. Refresh metadata before live research."
                )
            else:
                problems.extend(
                    validate_settings(
                        metadata.value,
                        SimulationSettings(
                            region=spec.region,
                            delay=spec.delay,
                            universe=spec.universe,
                            neutralization=spec.neutralization,
                            simulation_mode=QUICK_MODE,
                        ).model_dump(by_alias=True, exclude_none=True),
                    )
                )
            if not self.state.auth.session.authenticated:
                problems.append(
                    "BRAIN authentication is unavailable; sign in before live execution."
                )
            if not self.state.engine.quick_allowed:
                problems.append(
                    "BRAIN Quick-mode permission is required; Full is never silently substituted."
                )
            if not spec.templates and self.state.models.get(spec.model) is None:
                problems.append("Choose an existing configured provider model.")
            if not spec.templates and (
                spec.pricing is None or spec.pricing.verified_on > utcnow().date()
            ):
                problems.append(
                    "Record verified model pricing, including zero-cost pricing, "
                    "before authorizing requests."
                )
            if spec.pricing and (utcnow().date() - spec.pricing.verified_on).days > 30:
                problems.append(
                    "Model price verification is older than 30 days; verify current maximum rates."
                )
        used = Counter(h.dataset_id for h in history if h.document.get("region") == spec.region)
        opportunities: list[dict[str, Any]] = []
        for ctx in contexts:
            coverage = sum(f.coverage or 0 for f in ctx.fields) / max(1, len(ctx.fields))
            novelty = 1 / (1 + used[ctx.id])
            score = (
                0.4 * coverage
                + 0.4 * novelty
                + 0.1 * bool(ctx.category)
                + 0.1 * len({f.type for f in ctx.fields}) / 2
            )
            opportunities.append(
                {
                    "datasetId": ctx.id,
                    "name": ctx.name,
                    "category": ctx.category,
                    "fieldCount": len(ctx.fields),
                    "fieldTypes": sorted({f.type for f in ctx.fields}),
                    "score": round(score, 4),
                    "reason": f"Coverage {coverage:.0%}; {used[ctx.id]} prior mechanisms; "
                    "ranked by availability, novelty and typed coverage, not field count alone.",
                }
            )
        opportunities.sort(key=lambda item: (-item["score"], item["datasetId"]))
        # Keep a declared share for new mechanisms, with the remainder refining promising
        # datasets. Failed mechanisms still require an explicit revision at validation.
        if spec.exploration_fraction < 1:
            promising = {h.dataset_id for h in history if h.status == "PROMISING"}
            explore = [o for o in opportunities if o["datasetId"] not in promising]
            exploit = [o for o in opportunities if o["datasetId"] in promising]
            count = math.ceil(len(opportunities) * spec.exploration_fraction)
            opportunities = explore[:count] + exploit + explore[count:]
        return {
            "configHash": spec.digest(),
            "mode": spec.mode,
            "problems": problems,
            "warnings": [
                "Quick results are preliminary; no submission is automatic.",
                (
                    "Price bounds use your verified highest applicable token rates; "
                    "provider billing is not configured here."
                ),
            ]
            + (
                ["Multiple regional components must be validated independently."]
                if profile and profile.component_regions
                else []
            ),
            "opportunities": opportunities,
            "limits": spec.limits.model_dump(mode="json"),
            "requiresLiveApproval": spec.mode == "live",
            "profile": profile.model_dump(mode="json") if profile else None,
        }

    async def create(self, spec: CampaignSpec) -> int:
        await self.initialize()
        async with self.db.session() as session:
            frozen = await session.get(ResearchProfile, spec.profile_id)
        if frozen is None:
            raise ResearchError("Research profile does not exist.")
        spec = spec.model_copy(update={"profile_evidence": Profile.model_validate(frozen.document)})
        async with self.db.session() as session:
            row = ResearchCampaign(
                name=spec.name,
                mode=spec.mode,
                config=spec.model_dump(mode="json"),
                config_hash=spec.digest(),
                status="DRAFT",
                cursor={"stage": "DISCOVER", "cycle": 0},
            )
            session.add(row)
            await session.flush()
            study = Study(
                name=f"research-{row.id}-{row.config_hash[:12]}-{utcnow().timestamp()}",
                label=spec.name,
                sampler=SAMPLER,
                template_source="",
                objectives=["sharpe"],
                sampler_params={
                    "region": spec.region,
                    "delay": spec.delay,
                    "cores": 1,
                    "pilot": True,
                },
                task=f"research-{spec.mode}-{row.id}",
                status=StudyStatus.IDLE,
                max_trials=spec.limits.quick,
                batch_size=10,
            )
            session.add(study)
            await session.flush()
            row.study_id = study.id
            await self.event(
                session,
                row.id,
                "created",
                "created",
                {"configHash": row.config_hash, "mode": row.mode},
            )
            return row.id

    async def authorize(self, campaign_id: int, config_hash: str) -> None:
        async with self.db.session() as session:
            row = await session.get(ResearchCampaign, campaign_id)
            if row is None or row.mode != "live" or row.config_hash != config_hash:
                raise ResearchError("Approval must match the exact frozen live configuration.")
            row.authorized_hash = config_hash
            await self.event(
                session, row.id, "authorization", "user_authorization", {"configHash": config_hash}
            )

    async def control(self, campaign_id: int, action: str) -> None:
        row = await self.get(campaign_id)
        spec = CampaignSpec.model_validate(row.config)
        io = self._ios[row.mode]
        if action in {"start", "resume"}:
            if row.cursor.get("usage_overrun"):
                raise ResearchError(
                    "Usage exceeded its reserved bound; this campaign cannot spend further."
                )
            if row.status in {"COMPLETE", "CANCELLED"}:
                raise ResearchError("A completed or cancelled campaign cannot reset its budget.")
            if row.mode == "live" and row.authorized_hash != spec.digest():
                raise ResearchError(
                    "Explicit live approval of this exact configuration is required."
                )
            plan = await self.preview(spec)
            if plan["problems"]:
                async with self.db.session() as session:
                    stored = await session.get(ResearchCampaign, campaign_id)
                    stored.status, stored.message = "BLOCKED", plan["problems"][0]
                return
            status = "RUNNING"
        elif action in {"pause", "cancel"}:
            status = "PAUSED" if action == "pause" else "CANCELLED"
        else:
            raise ResearchError("Unknown campaign control action.")
        async with self.db.session() as session:
            stored = await session.get(ResearchCampaign, campaign_id)
            stored.status, stored.retry_at, stored.message = status, None, None
            study = await session.get(Study, stored.study_id)
            study.status = StudyStatus.RUNNING if status == "RUNNING" else StudyStatus.PAUSED
            await self.event(session, campaign_id, f"{action}:{utcnow().isoformat()}", action, {})
        if status != "RUNNING":
            async with self.db.session() as session:
                study = await session.get(Study, row.study_id)
                task = study.task
            await io.engine.set_quota(task, 0)
            await io.engine.drop_queued(task)
            if row.study_id is not None:
                await scheduler.prune_unsent(io.optimizer, row.study_id)
            await self._settle(row)

    async def advance(self, campaign_id: int) -> None:
        async with self._locks.setdefault(campaign_id, asyncio.Lock()):
            row = await self.get(campaign_id)
            if row.status == "BLOCKED" and row.retry_at and row.retry_at <= utcnow():
                async with self.db.session() as session:
                    stored = await session.get(ResearchCampaign, campaign_id)
                    stored.status, stored.retry_at = "RUNNING", None
                row = await self.get(campaign_id)
            if row.status != "RUNNING":
                if row.study_id:
                    await self._settle(row)
                return
            try:
                await self._step(row, CampaignSpec.model_validate(row.config))
                async with self.db.session() as session:
                    await session.execute(
                        update(ResearchCampaign)
                        .where(ResearchCampaign.id == row.id)
                        .values(updated_at=utcnow())
                    )
            except BudgetExhaustedError as exc:
                await self._block(
                    row,
                    "Provider request quota unavailable; no generation was dispatched.",
                    max(5, int(exc.retry_after)),
                )
            except budget.BudgetBlocked as exc:
                await self._block(row, str(exc), exc.retry_after)
            except (LLMError, ValidationError) as exc:
                await self._block(row, "Provider generation failed safely: " + type(exc).__name__)
            except Exception as exc:  # noqa: BLE001 — fail closed without logging secrets
                # Raw transport/provider payloads may contain research content. The event
                # stores the class and phase; no credential or response body is logged.
                await self._block(
                    row,
                    "Infrastructure failure in "
                    + str(row.cursor.get("stage"))
                    + ": "
                    + type(exc).__name__,
                )

    async def _block(self, row: ResearchCampaign, reason: str, retry: int | None = None) -> None:
        async with self.db.session() as session:
            stored = await session.get(ResearchCampaign, row.id)
            if stored.status == "RUNNING":
                stored.status, stored.message = "BLOCKED", reason
                stored.retry_at = utcnow() + timedelta(seconds=retry) if retry else None
                await self.event(
                    session,
                    row.id,
                    f"blocked:{utcnow().isoformat()}",
                    "blocked",
                    {"reason": reason, "retryAfter": retry},
                )

    async def _step(self, row: ResearchCampaign, spec: CampaignSpec) -> None:
        stage = row.cursor.get("stage", "DISCOVER")
        if stage == "DISCOVER":
            preview = await self.preview(spec)
            if preview["problems"]:
                raise budget.BudgetBlocked(preview["problems"][0])
            async with self.db.session() as session:
                await self.event(
                    session,
                    row.id,
                    f"discovery:{row.cursor['cycle']}",
                    "discovery",
                    {"backlog": preview["opportunities"]},
                )
            await self._cursor(
                row.id, stage="GENERATE", backlog=preview["opportunities"], generated=[]
            )
        elif stage == "GENERATE":
            await self._generate(row, spec)
        elif stage in {"QUICK", "FULL"}:
            await self._dispatch(row, spec, stage)
        elif stage in {"QUICK_WAIT", "FULL_WAIT"}:
            await self._wait(row, spec, stage.split("_")[0])
        elif stage == "PROMOTE":
            await self._promote(row, spec)
        elif stage == "EVALUATE":
            await self._evaluate(row, spec)
        elif stage == "DECIDE":
            await self._decide(row, spec)
        else:
            raise ResearchError("Unknown persisted research phase.")

    async def _generate(self, row: ResearchCampaign, spec: CampaignSpec) -> None:
        io = self._ios[row.mode]
        contexts = {ctx.id: ctx for ctx in await io.contexts(spec)}
        done = list(row.cursor.get("generated", []))
        maximum = (
            min(len(contexts), max(1, spec.limits.requests // spec.cycles))
            if not spec.templates
            else len(contexts)
        )
        wanted = [item["datasetId"] for item in row.cursor.get("backlog", [])][:maximum]
        dataset = next((key for key in wanted if key not in done), None)
        if dataset is None:
            await self._cursor(row.id, stage="QUICK")
            return
        if dataset not in contexts:
            raise budget.BudgetBlocked("Cached dataset disappeared; sync metadata before resuming.")
        ctx = contexts[dataset]
        operators = await io.operators()
        async with self.db.session() as session:
            memory_rows = list(
                await session.scalars(
                    select(ResearchHypothesis)
                    .where(
                        ResearchHypothesis.dataset_id == dataset,
                        ResearchHypothesis.status.in_(
                            ["FAILED", "PROMISING", "INFRASTRUCTURE_BLOCKED", "EVIDENCE_INCOMPLETE"]
                        ),
                    )
                    .order_by(ResearchHypothesis.id.desc())
                    .limit(12)
                )
            )
        memory = json.dumps(
            {
                "explorationFraction": spec.exploration_fraction,
                "revisionReason": spec.revision_reason,
                "hypotheses": [
                    {
                        "mechanism": h.document.get("mechanism"),
                        "family": h.family,
                        "outcome": h.status,
                    }
                    for h in memory_rows
                ],
                "instruction": (
                    "Refine promising mechanisms with small numeric parameter "
                    "changes; explore distinct families for the remaining "
                    "allocation. Infrastructure failures are not negative "
                    "economic evidence."
                ),
            }
        )
        prompt = io.prompt(spec, ctx, operators, memory)
        operation = f"generation:{row.cursor['cycle']}:{dataset}"
        if spec.templates:
            try:
                generated = await self._templates(spec, ctx, operator_table(operators))
            except (ValidationError, ResearchError, ImportRefused) as exc:
                async with self.db.session() as session:
                    await self.event(
                        session,
                        row.id,
                        operation + ":empty",
                        "template_refused",
                        {
                            "dataset": dataset,
                            "reason": str(exc)
                            if isinstance(exc, ResearchError | ImportRefused)
                            else "No renderable templates in this market.",
                        },
                    )
                await self._cursor(row.id, generated=[*done, dataset])
                return
            observed = {}
        else:
            if row.mode == "live":
                model = self.state.models.get(spec.model)
                await self.state.llm.keys._pick(model, set())
                if len(SYSTEM + prompt) // 4 > model.prompt_tokens:
                    raise budget.BudgetBlocked(
                        "Cached fields exceed this model's prompt budget; narrow the campaign."
                    )
            input_bound = (
                len((SYSTEM + prompt + json.dumps(Generated.model_json_schema())).encode()) + 4096
            )
            price = spec.pricing
            cost = (
                math.ceil(
                    (
                        input_bound * price.input_microusd_per_million
                        + spec.max_output_tokens * price.output_microusd_per_million
                    )
                    / 1_000_000
                )
                if price
                else 0
            )
            first = await budget.reserve(
                self.db,
                row.id,
                operation,
                {"requests": 1, "tokens": input_bound + spec.max_output_tokens, "microusd": cost},
            )
            if not first:
                # A response may have been lost after the charge. Do not send that request
                # again or invent its contents. The next dataset may use remaining budget.
                async with self.db.session() as session:
                    await self.event(
                        session,
                        row.id,
                        operation + ":unknown",
                        "interrupted_generation",
                        {
                            "dataset": dataset,
                            "reason": "Reserved request has no saved result; slot retained.",
                        },
                    )
                await self._cursor(row.id, generated=[*done, dataset])
                return
            try:
                generated, observed = await io.generate(spec, ctx, prompt)
            except (LLMError, ValidationError) as exc:
                async with self.db.session() as session:
                    await self.event(
                        session,
                        row.id,
                        operation + ":failed",
                        "unsupported_model_response"
                        if isinstance(exc, ValidationError)
                        else "provider_failure",
                        {"dataset": dataset, "errorClass": type(exc).__name__},
                    )
                await budget.observed(self.db, row.id, operation, {})
                await self._cursor(row.id, generated=[*done, dataset])
                return
            if price and "input" in observed:
                observed["microusd"] = math.ceil(
                    (
                        observed["input"] * price.input_microusd_per_million
                        + observed["output"] * price.output_microusd_per_million
                    )
                    / 1_000_000
                )
            await budget.observed(self.db, row.id, operation, observed)
        await self._save_generation(row, spec, ctx, generated, operator_table(operators), operation)
        await self._cursor(row.id, generated=[*done, dataset])

    async def _templates(
        self, spec: CampaignSpec, ctx: Context, operators: dict[str, Any]
    ) -> Generated:
        mechanism = Hypothesis(
            mechanism=spec.hypothesis,
            family="declared_template",
            direction="User-declared; not inferred.",
            horizon="Declared parameter grid",
            assumptions=["User's template rationale"],
            fields_required=[f.id for f in ctx.fields[:1]],
            risk_factors=["Unverified until evaluated"],
            failure_regimes=["Non-positive after-cost performance"],
            falsification_tests=["Independent Full and validation results"],
            independence="Must be checked against existing expressions and submitted baseline.",
        )
        alphas = []
        for document in spec.templates:
            # Reuse the same data-only validation as notebook imports; no notebook code runs.
            preview_notebook(
                json.dumps(
                    {
                        "nbformat": 4,
                        "cells": [],
                        "metadata": {
                            "alpha_harness": {"template_lab": [document.model_dump(mode="json")]}
                        },
                    }
                )
            )
            variables = [
                {
                    "name": key,
                    "kind": value.kind,
                    "vectorOptions": {
                        name: list(info.explicit)
                        for name, info in operators.items()
                        if info.explicit
                    },
                }
                for key, value in document.variables.items()
            ]
            spaces: list[list[dict[str, Any]]] = []
            for key, variable in document.variables.items():
                if isinstance(variable, FieldsVariable):
                    if variable.filter is not None:
                        raise ResearchError(
                            "Narrow imported field filters in Data Explorer before rendering "
                            "this template."
                        )
                    fields = await self._ios[spec.mode].template_fields(spec, ctx, variable)

                    vector_only = variable.filter is not None and variable.filter.field_types == [
                        "VECTOR"
                    ]
                    choices = [
                        {f"${key}": f.id}
                        for f in fields
                        if f.type == "MATRIX" or (vector_only and f.type == "VECTOR")
                    ]
                    choices += [
                        {f"${key}": f.id, f"${key}#vector": operator}
                        for f in fields
                        if f.type == "VECTOR" and not vector_only
                        for operator in variable.vector_operators
                    ]
                else:
                    choices = [{f"${key}": value} for value in template.values(variable.values)]
                spaces.append(choices)
            for combination in itertools.islice(itertools.product(*spaces), 40 - len(alphas)):
                params = {key: value for part in combination for key, value in part.items()}
                expression = template.render(document.text, {"variables": variables}, params)
                alphas.append(GeneratedAlpha(expression=expression, hypothesis=0))
        return Generated(hypotheses=[mechanism], alphas=alphas[:40])

    async def _save_generation(
        self,
        row: ResearchCampaign,
        spec: CampaignSpec,
        ctx: Context,
        generated: Generated,
        operators: dict[str, Any],
        operation: str,
    ) -> None:
        async with self.db.session() as session:
            existing = list(
                await session.scalars(
                    select(ResearchCandidate).where(ResearchCandidate.campaign_id == row.id)
                )
            )
            identities = {c.identity for c in existing if c.status != "REJECTED"}
            hypotheses = []
            for document in generated.hypotheses:
                data = document.model_dump(mode="json") | {
                    "region": spec.region,
                    "dataset": ctx.id,
                    "cycle": row.cursor["cycle"],
                }
                identity = hashlib.sha256(
                    json.dumps(
                        {
                            "mechanism": document.mechanism.casefold(),
                            "dataset": ctx.id,
                            "region": spec.region,
                        },
                        sort_keys=True,
                    ).encode()
                ).hexdigest()
                hypothesis = await session.scalar(
                    select(ResearchHypothesis).where(
                        ResearchHypothesis.campaign_id == row.id,
                        ResearchHypothesis.identity == identity,
                    )
                )
                if hypothesis is None:
                    hypothesis = ResearchHypothesis(
                        campaign_id=row.id,
                        identity=identity,
                        dataset_id=ctx.id,
                        family=document.family,
                        document=data,
                    )
                    session.add(hypothesis)
                    await session.flush()
                hypotheses.append(hypothesis)
            counts: Counter[str] = Counter()
            for alpha in generated.alphas:
                hypothesis = hypotheses[alpha.hypothesis]
                original, expression = alpha.expression, alpha.expression
                status, reason, used = "VALIDATED", None, frozenset()
                identity = hashlib.sha256(original.encode()).hexdigest()
                structure, complexity = identity, 0
                try:
                    if set(generated.hypotheses[alpha.hypothesis].fields_required) - ctx.names:
                        raise Rejected("Hypothesis requires fields absent from this cached market.")
                    old_failure = await session.scalar(
                        select(ResearchHypothesis.id).where(
                            ResearchHypothesis.identity == hypothesis.identity,
                            ResearchHypothesis.status == "FAILED",
                            ResearchHypothesis.campaign_id != row.id,
                        )
                    )
                    if old_failure and not spec.revision_reason:
                        raise Rejected("Previously failed mechanism requires a justified revision.")
                    candidate = repair_temporal_vector(original, ctx.types)
                    if candidate != original and not spec.accept_structural_repairs:
                        raise Rejected(
                            "Structural repair requires explicit permission; original retained."
                        )
                    expression, used = check(
                        candidate, ctx, operators, enforce_power_pool=spec.purpose == "power_pool"
                    )
                    identity, structure, complexity = evidence.identities(expression, ctx.names)
                    max_ops = spec.profile_evidence.max_operators if spec.profile_evidence else 32
                    max_fields = spec.profile_evidence.max_fields if spec.profile_evidence else 12
                    if complexity > max_ops or len(used) > max_fields:
                        raise Rejected(
                            "Campaign expression complexity exceeds 32 operators or 12 fields."
                        )
                    if identity in identities:
                        raise Rejected("Exact canonical duplicate in this campaign.")
                    if any(spec.universe not in ctx.held.get(field, ()) for field in used):
                        raise Rejected(
                            "A referenced field is unavailable in the selected universe."
                        )
                    identities.add(identity)
                    if candidate != original:
                        reason = (
                            "Explicitly permitted narrow VECTOR structural repair; economic "
                            "equivalence is not assumed."
                        )
                except Rejected as exc:
                    status, reason = "REJECTED", str(exc)
                counts[status] += 1
                session.add(
                    ResearchCandidate(
                        campaign_id=row.id,
                        hypothesis_id=hypothesis.id,
                        original=original,
                        expression=expression,
                        identity=identity,
                        structure=structure,
                        settings=SimulationSettings(
                            region=spec.region,
                            delay=spec.delay,
                            universe=spec.universe,
                            neutralization=spec.neutralization,
                            simulation_mode=QUICK_MODE,
                        ).model_dump(by_alias=True, exclude_none=True),
                        status=status,
                        reason=reason,
                        provenance={
                            "dataset": ctx.id,
                            "fields": sorted(used),
                            "fieldCoverage": {
                                f.id: f.coverage for f in (*ctx.fields, *ctx.basics) if f.id in used
                            },
                            "family": hypothesis.family,
                            "model": spec.model if row.mode == "live" else "synthetic",
                            "cycle": row.cursor["cycle"],
                            "operators": complexity,
                            "templateSourceHash": spec.template_source_hash,
                        },
                    )
                )
            await self.event(
                session,
                row.id,
                operation + ":saved",
                "generated",
                {"dataset": ctx.id, "counts": dict(counts)},
            )

    async def _dispatch(self, row: ResearchCampaign, spec: CampaignSpec, phase: str) -> None:
        io = self._ios[row.mode]
        id_key = phase.lower() + "_ids"
        ids = list(row.cursor.get(id_key, []))
        if phase == "QUICK" and not ids:
            spent = await budget.usage(self.db, row.id, row.mode)
            target = min(
                10,
                math.ceil(spec.limits.quick / spec.cycles),
                max(0, spec.limits.quick - spent.get("quick", 0)),
            )
            async with self.db.session() as session:
                proposals = list(
                    await session.scalars(
                        select(ResearchCandidate).where(
                            ResearchCandidate.campaign_id == row.id,
                            ResearchCandidate.status == "VALIDATED",
                        )
                    )
                )
                proposals = [
                    c for c in proposals if c.provenance.get("cycle") == row.cursor["cycle"]
                ]
                previous = list(
                    await session.scalars(
                        select(ResearchCandidate).where(
                            ResearchCandidate.campaign_id == row.id,
                            ResearchCandidate.quick_trial_id.is_not(None),
                        )
                    )
                )
                datasets = Counter(c.provenance["dataset"] for c in previous)
                families: Counter[int] = Counter()
                structures: Counter[str] = Counter()
                picked = []
                while proposals and len(picked) < target:
                    chosen = min(
                        proposals,
                        key=lambda c: (
                            datasets[c.provenance["dataset"]],
                            families[c.hypothesis_id],
                            structures[c.structure],
                            c.provenance.get("operators", 0),
                            c.id,
                        ),
                    )
                    proposals.remove(chosen)
                    picked.append(chosen)
                    datasets[chosen.provenance["dataset"]] += 1
                    families[chosen.hypothesis_id] += 1
                    structures[chosen.structure] += 1
                last = await session.scalar(
                    select(func.max(Trial.number)).where(Trial.study_id == row.study_id)
                )
                number = int(last) + 1 if last is not None else 0
                for candidate in picked:
                    settings = SimulationSettings(
                        region=spec.region,
                        delay=spec.delay,
                        universe=spec.universe,
                        neutralization=spec.neutralization,
                        simulation_mode=QUICK_MODE,
                        visualization=False,
                    )
                    trial = Trial(
                        study_id=row.study_id,
                        number=number,
                        expression=candidate.expression,
                        settings=settings.model_dump(by_alias=True, exclude_none=True),
                        distributions={},
                        params={
                            "researchCandidate": candidate.id,
                            "dataset": candidate.provenance["dataset"],
                            "cycle": row.cursor["cycle"],
                        },
                        state=TrialState.PRUNED,
                        message="Frozen research candidate; waiting for reserved dispatch.",
                    )
                    session.add(trial)
                    await session.flush()
                    candidate.quick_trial_id, candidate.settings, candidate.status = (
                        trial.id,
                        trial.settings,
                        "QUICK_READY",
                    )
                    candidate.provenance = candidate.provenance | {
                        "quickReason": (
                            "Fair dataset allocation, hypothesis diversity and structural "
                            "novelty before simulation."
                        )
                    }
                    ids.append(trial.id)
                    number += 1
                stored = await session.get(ResearchCampaign, row.id)
                stored.cursor = stored.cursor | {id_key: ids}
                await self.event(
                    session,
                    row.id,
                    f"quick-selection:{row.cursor['cycle']}",
                    "quick_selection",
                    {
                        "target": target,
                        "selected": len(ids),
                        "allocation": dict(datasets),
                        "shortfallReason": "Insufficient distinct validated candidates"
                        if len(ids) < target
                        else None,
                    },
                )
        if not ids:
            await self._cursor(row.id, stage="PROMOTE" if phase == "QUICK" else "EVALUATE")
            return
        async with self.db.session() as session:
            trials = list(
                await session.scalars(select(Trial).where(Trial.id.in_(ids)).order_by(Trial.number))
            )
            study = await session.get(Study, row.study_id)
        ready = [trial for trial in trials if trial.state == TrialState.PRUNED]
        if ready and row.mode == "live":
            preflight = await self.preview(spec)
            if preflight["problems"]:
                raise budget.BudgetBlocked(preflight["problems"][0])
        if ready:
            # The group is immutable; a pause reuses its reservation and dispatch identities.
            await budget.reserve(
                self.db, row.id, f"{phase.lower()}:{row.cursor['cycle']}", {phase.lower(): len(ids)}
            )
            current = await self.get(row.id)
            if current.status != "RUNNING":
                return
            await io.engine.set_quota(study.task, 1)
            requests = [
                SimulationRequest(
                    regular=t.expression, settings=SimulationSettings.model_validate(t.settings)
                )
                for t in ready
            ]
            outcomes = (
                await io.engine.enqueue(
                    requests,
                    task=study.task,
                    allow_uncertain_retry=False,
                    intent_keys=[
                        f"research:{row.id}:{row.config_hash}:trial:{t.id}" for t in ready
                    ],
                )
            )["outcomes"]
            if len(outcomes) != len(ready):
                raise ResearchError(
                    "Engine returned an inconsistent dispatch result; trial identities retained."
                )
            async with self.db.session() as session:
                for detached, outcome in zip(ready, outcomes, strict=True):
                    trial = await session.get(Trial, detached.id)
                    if not outcome.get("recordId"):
                        raise ResearchError("Dispatch returned no persistent simulation record.")
                    trial.state, trial.simulation_record_id = TrialState.QUEUED, outcome["recordId"]
                    trial.alpha_id = outcome.get("alphaId")
                    trial.message = None
                await self.event(
                    session,
                    row.id,
                    f"dispatch:{phase}:{row.cursor['cycle']}",
                    "dispatch",
                    {"phase": phase, "trials": ids, "synthetic": row.mode == "offline"},
                )
        await self._cursor(row.id, stage=phase + "_WAIT")
        if (await self.get(row.id)).status != "RUNNING":
            await io.engine.set_quota(study.task, 0)
            await io.engine.drop_queued(study.task)
            if row.study_id is not None:
                await scheduler.prune_unsent(io.optimizer, row.study_id)

    async def _uncertain(self, trials: list[Trial]) -> bool:
        failed = [t for t in trials if t.state == TrialState.FAIL]
        if not failed:
            return False
        async with self.db.session() as session:
            records = {
                r.id: r
                for r in await session.scalars(
                    select(SimulationRecord).where(
                        SimulationRecord.id.in_(
                            [t.simulation_record_id for t in failed if t.simulation_record_id]
                        )
                    )
                )
            }
        for trial in failed:
            record = records.get(trial.simulation_record_id)
            if record is None or (
                record.sent_at is not None and not record.platform_id and not record.alpha_id
            ):
                return True
            if any(
                word in (trial.message or "").lower() for word in ["uncertain", "answer was lost"]
            ):
                return True
        return False

    async def _settle(self, row: ResearchCampaign) -> None:
        if row.study_id is None:
            return
        io = self._ios[row.mode]
        async with self.db.session() as session:
            open_count = await session.scalar(
                select(func.count())
                .select_from(Trial)
                .where(
                    Trial.study_id == row.study_id,
                    Trial.state.in_([TrialState.QUEUED, TrialState.RUNNING]),
                )
            )
        if open_count:
            await io.harvest(row.study_id)
        async with self.db.session() as session:
            candidates = list(
                await session.scalars(
                    select(ResearchCandidate).where(ResearchCandidate.campaign_id == row.id)
                )
            )
            trial_ids = [
                identity
                for c in candidates
                for identity in [c.quick_trial_id, c.full_trial_id]
                if identity
            ]
            trials = {
                t.id: t for t in await session.scalars(select(Trial).where(Trial.id.in_(trial_ids)))
            }
            for candidate in candidates:
                full = trials.get(candidate.full_trial_id)
                quick = trials.get(candidate.quick_trial_id)
                latest = full or quick
                if latest and latest.state in {TrialState.COMPLETE, TrialState.FAIL}:
                    candidate.status = ("FULL" if full else "QUICK") + (
                        "_COMPLETE" if latest.state == TrialState.COMPLETE else "_FAILED"
                    )
                    if latest.state == TrialState.FAIL:
                        candidate.reason = (
                            latest.message
                            or "Simulation infrastructure failed; hypothesis quality is unknown."
                        )
            for phase in ["quick", "full"]:
                ids = row.cursor.get(phase + "_ids", [])
                current = [trials[identity] for identity in ids if identity in trials]
                if current and not any(
                    t.state in {TrialState.QUEUED, TrialState.RUNNING} for t in current
                ):
                    uncertain = await self._uncertain(current)
                    if not uncertain:
                        await session.execute(
                            update(ResearchReservation)
                            .where(
                                ResearchReservation.campaign_id == row.id,
                                ResearchReservation.key == f"{phase}:{row.cursor['cycle']}",
                            )
                            .values(
                                settled=True,
                                actual=sum(t.state == TrialState.COMPLETE for t in current),
                            )
                        )

    async def _wait(self, row: ResearchCampaign, _spec: CampaignSpec, phase: str) -> None:
        await self._settle(row)
        ids = row.cursor.get(phase.lower() + "_ids", [])
        async with self.db.session() as session:
            trials = list(await session.scalars(select(Trial).where(Trial.id.in_(ids))))
        if any(t.state in {TrialState.QUEUED, TrialState.RUNNING} for t in trials):
            return
        if any(t.state == TrialState.PRUNED for t in trials):
            await self._cursor(row.id, stage=phase)
            return
        if await self._uncertain(trials):
            raise budget.BudgetBlocked(
                "Uncertain BRAIN outcome: inspect the platform before releasing "
                "the global concurrency reservation."
            )
        await self._cursor(row.id, stage="PROMOTE" if phase == "QUICK" else "EVALUATE")

    async def _promote(self, row: ResearchCampaign, spec: CampaignSpec) -> None:
        spent = await budget.usage(self.db, row.id, row.mode)
        cap = min(
            max(0, spec.limits.full - spent.get("full", 0)),
            max(1, math.ceil(spec.limits.full / spec.cycles)),
        )
        async with self.db.session() as session:
            rows = list(
                await session.scalars(
                    select(ResearchCandidate).where(
                        ResearchCandidate.campaign_id == row.id,
                        ResearchCandidate.quick_trial_id.is_not(None),
                        ResearchCandidate.full_trial_id.is_(None),
                    )
                )
            )
            rows = [c for c in rows if c.provenance.get("cycle") == row.cursor["cycle"]]
            trials = list(
                await session.scalars(
                    select(Trial).where(Trial.id.in_([c.quick_trial_id for c in rows]))
                )
            )
        choices = {choice.number: choice for choice in quick_choices(trials)}
        by_trial = {t.id: t for t in trials}
        eligible = []
        for candidate in rows:
            trial = by_trial[candidate.quick_trial_id]
            if trial.number not in choices:
                continue
            stats = (trial.result or {}).get("stats") or {}
            turnover = stats.get("turnover")
            if (
                not isinstance(turnover, int | float)
                or isinstance(turnover, bool)
                or not math.isfinite(turnover)
                or turnover < 0
            ):
                async with self.db.session() as session:
                    current = await session.get(ResearchCandidate, candidate.id)
                    current.reason = (
                        "Finite nonnegative turnover is required for cost-aware promotion."
                    )
                continue
            weights = spec.promotion_weights
            novelty = 1 / max(1, sum(c.structure == candidate.structure for c in rows))
            components = {
                "fitness": float(stats["fitness"]) * weights.fitness,
                "sharpe": float(stats["sharpe"]) * weights.sharpe,
                "complexity": -candidate.provenance.get("operators", 0) * weights.complexity,
                "turnover": -float(turnover) * weights.turnover,
                "novelty": novelty * weights.novelty,
            }
            eligible.append((candidate, trial, sum(components.values()), components))
        eligible.sort(key=lambda item: (-item[2], item[0].id))
        ids = [str(trial.alpha_id) for _, trial, _, _ in eligible if trial.alpha_id]
        series = await self._ios[row.mode].vault.series(ids)
        picked: list[Any] = []
        skipped: dict[int, str] = {}
        datasets: set[str] = set()
        while eligible and len(picked) < cap:
            option = next(
                (item for item in eligible if item[0].provenance["dataset"] not in datasets),
                eligible[0],
            )
            eligible.remove(option)
            candidate, trial, _, _ = option
            redundant = False
            for _, other, _, _ in picked:
                value, _ = evidence.correlation(
                    series.get(trial.alpha_id, {}), series.get(other.alpha_id, {}), spec
                )
                if value is not None and abs(value) > spec.max_discovery_correlation:
                    redundant = True
            if redundant:
                skipped[candidate.id] = "Redundant discovery correlation; Full quota preserved."
                continue
            picked.append(option)
            datasets.add(candidate.provenance["dataset"])
        async with self.db.session() as session:
            last = await session.scalar(
                select(func.max(Trial.number)).where(Trial.study_id == row.study_id)
            )
            number = int(last) + 1 if last is not None else 0
            full_ids = []
            for detached, quick, score, components in picked:
                candidate = await session.get(ResearchCandidate, detached.id)
                trial = Trial(
                    study_id=row.study_id,
                    number=number,
                    expression=quick.expression,
                    settings={**quick.settings, "simulationMode": FULL_MODE},
                    distributions={},
                    params={
                        "fullOf": quick.number,
                        "researchCandidate": candidate.id,
                        "dataset": candidate.provenance["dataset"],
                        "score": score,
                        "components": components,
                        "reason": (
                            "Passing preliminary checks; configurable quality, novelty and "
                            "dataset diversity. Full and robustness checks remain required."
                        ),
                    },
                    state=TrialState.PRUNED,
                    message=scheduler.PENDING_FULL,
                )
                session.add(trial)
                await session.flush()
                candidate.full_trial_id, candidate.status = trial.id, "FULL_READY"
                candidate.provenance = candidate.provenance | {"promotion": trial.params}
                full_ids.append(trial.id)
                number += 1
            promoted = {item[0].id for item in picked}
            for detached in rows:
                if detached.id not in promoted:
                    candidate = await session.get(ResearchCandidate, detached.id)
                    candidate.reason = skipped.get(candidate.id) or (
                        "Quick checks/finite positive Fitness and Sharpe did not pass."
                        if by_trial[candidate.quick_trial_id].number not in choices
                        else "Below the capped, diversified Full allocation."
                    )
            stored = await session.get(ResearchCampaign, row.id)
            stored.cursor = stored.cursor | {"full_ids": full_ids, "stage": "FULL"}
            await self.event(
                session,
                row.id,
                f"promotions:{row.cursor['cycle']}",
                "promotion",
                {
                    "eligible": len(choices),
                    "promoted": len(full_ids),
                    "cap": cap,
                    "skippedCorrelation": len(skipped),
                    "emptyReason": "No qualifying Quick candidates or no Full budget"
                    if not full_ids
                    else None,
                },
            )

    async def _evaluate(self, row: ResearchCampaign, spec: CampaignSpec) -> None:
        io = self._ios[row.mode]
        if spec.validation_end >= utcnow().date():
            raise budget.BudgetBlocked(
                "Validation window is still open. Refresh cached evidence after it closes."
            )
        async with self.db.session() as session:
            candidates = list(
                await session.scalars(
                    select(ResearchCandidate).where(
                        ResearchCandidate.campaign_id == row.id,
                        ResearchCandidate.full_trial_id.is_not(None),
                    )
                )
            )
            fulls = {
                t.id: t
                for t in await session.scalars(
                    select(Trial).where(Trial.id.in_([c.full_trial_id for c in candidates]))
                )
            }
            exposures = list(
                await session.scalars(
                    select(ResearchExposure).where(
                        ResearchExposure.region == spec.region,
                        ResearchExposure.campaign_id != row.id,
                        ResearchExposure.start <= spec.validation_end.isoformat(),
                        ResearchExposure.end >= spec.validation_start.isoformat(),
                    )
                )
            )
            tested = int(
                await session.scalar(
                    select(func.count())
                    .select_from(ResearchCandidate)
                    .where(ResearchCandidate.campaign_id == row.id)
                )
                or 0
            )
        ids = [t.alpha_id for t in fulls.values() if t.alpha_id]
        pool_rows: dict[str, dict[str, Any]] = {}
        if row.mode == "live" and spec.purpose == "power_pool" and ids:
            # Local Vault computation only; never sync or fetch additional alphas here.
            from ..api.lab_tasks import local_power_pool

            measured = await local_power_pool(self.state, ids)
            pool_rows = {r.alpha_id: r.model_dump(mode="json") for r in measured.rows}
        async with self.db.session() as session:
            all_candidates = list(
                await session.scalars(
                    select(ResearchCandidate).where(ResearchCandidate.campaign_id == row.id)
                )
            )
            quicks = {
                t.id: t
                for t in await session.scalars(
                    select(Trial).where(
                        Trial.id.in_([c.quick_trial_id for c in all_candidates if c.quick_trial_id])
                    )
                )
            }
        stored = await io.vault.by_ids([*ids, *spec.baseline_ids])
        series = await io.vault.series([*ids, *spec.baseline_ids])
        submitted = await io.vault.submitted_members()
        selected = [
            fulls[c.full_trial_id].alpha_id
            for c in candidates
            if c.evidence
            and c.evidence.get("recommendation") == "approve_for_manual_review"
            and fulls[c.full_trial_id].alpha_id
        ]
        ordered = sorted(
            candidates,
            key=lambda c: (-float(c.provenance.get("promotion", {}).get("score", 0)), c.id),
        )
        for candidate in ordered:
            if candidate.evidence is not None:
                continue
            trial = fulls[candidate.full_trial_id]
            if trial.state != TrialState.COMPLETE or not trial.alpha_id:
                report = {
                    "recommendation": "needs_evidence_or_reject",
                    "confidence": "insufficient",
                    "reasons": [trial.message or "Full simulation did not complete."],
                    "submissionReady": False,
                    "synthetic": row.mode == "offline",
                    "failureClass": "infrastructure_failure",
                }
            else:
                report = await asyncio.to_thread(
                    evidence.assess,
                    spec,
                    trial.alpha_id,
                    stored,
                    series,
                    submitted,
                    tested,
                    reused_window=bool(exposures)
                    or int(row.cursor["cycle"]) > 0
                    or bool(row.cursor.get("evidence_version")),
                    created_before_validation=row.created_at.date() < spec.validation_start,
                    other_selected=selected,
                    pool_rows=pool_rows,
                    field_coverage=candidate.provenance.get("fieldCoverage"),
                    sensitivity=[
                        {
                            "candidateId": c.id,
                            "trialNumber": quicks[c.quick_trial_id].number,
                            "settings": quicks[c.quick_trial_id].settings,
                            "expression": c.expression,
                            "stats": (quicks[c.quick_trial_id].result or {}).get("stats"),
                            "passed": bool(quick_choices([quicks[c.quick_trial_id]])),
                            "mode": "QUICK",
                        }
                        for c in all_candidates
                        if c.id != candidate.id
                        and c.structure == candidate.structure
                        and evidence.parameter_neighbor(candidate.expression, c.expression)
                        and c.provenance["dataset"] == candidate.provenance["dataset"]
                        and c.quick_trial_id in quicks
                    ],
                )
                if report["recommendation"] == "approve_for_manual_review":
                    selected.append(trial.alpha_id)
            async with self.db.session() as session:
                current = await session.get(ResearchCandidate, candidate.id)
                current.evidence = report
                await self.event(
                    session,
                    row.id,
                    f"evidence:{candidate.id}:{row.cursor.get('evidence_version', 0)}",
                    "evidence",
                    {"candidateId": candidate.id, "report": report},
                )
        async with self.db.session() as session:
            hypotheses = list(
                await session.scalars(
                    select(ResearchHypothesis).where(ResearchHypothesis.campaign_id == row.id)
                )
            )
            for dataset in {h.dataset_id for h in hypotheses}:
                session.add(
                    ResearchExposure(
                        campaign_id=row.id,
                        region=spec.region,
                        dataset_id=dataset,
                        start=spec.validation_start.isoformat(),
                        end=spec.validation_end.isoformat(),
                        purpose="validation",
                    )
                )
            await self.event(
                session,
                row.id,
                f"evaluation:{row.cursor['cycle']}:{row.cursor.get('evidence_version', 0)}",
                "evaluation",
                {
                    "fullCandidates": len(candidates),
                    "recommended": len(selected),
                    "emptyReason": "No qualifying Full evidence; rejection reasons retained."
                    if not selected
                    else None,
                },
            )
        await self._cursor(row.id, stage="DECIDE", recommended=len(selected))

    async def _decide(self, row: ResearchCampaign, spec: CampaignSpec) -> None:
        spent = await budget.usage(self.db, row.id, row.mode)
        async with self.db.session() as session:
            candidates = list(
                await session.scalars(
                    select(ResearchCandidate).where(ResearchCandidate.campaign_id == row.id)
                )
            )
            hypotheses = list(
                await session.scalars(
                    select(ResearchHypothesis).where(ResearchHypothesis.campaign_id == row.id)
                )
            )
            by_hypothesis: dict[int, list[ResearchCandidate]] = {}
            taxonomy: Counter[str] = Counter()
            for candidate in candidates:
                by_hypothesis.setdefault(candidate.hypothesis_id, []).append(candidate)
                if candidate.status == "REJECTED":
                    taxonomy[
                        "duplicate"
                        if "duplicate" in (candidate.reason or "").lower()
                        else "invalid_expression"
                    ] += 1
                elif candidate.status.endswith("FAILED"):
                    taxonomy["infrastructure_failure"] += 1
                elif candidate.evidence:
                    kind = candidate.evidence.get("failureClass")
                    if kind:
                        taxonomy[str(kind)] += 1
            for hypothesis in hypotheses:
                own = by_hypothesis.get(hypothesis.id, [])
                if any(
                    c.evidence and c.evidence.get("recommendation") == "approve_for_manual_review"
                    for c in own
                ):
                    hypothesis.status = "PROMISING"
                elif any(c.status.endswith("FAILED") for c in own):
                    hypothesis.status = "INFRASTRUCTURE_BLOCKED"
                elif any(
                    c.evidence
                    and c.evidence.get("failureClass")
                    in {"weak_performance", "excessive_correlation", "constraint_violation"}
                    for c in own
                ):
                    hypothesis.status = "FAILED"
                elif any(c.evidence for c in own):
                    hypothesis.status = "EVIDENCE_INCOMPLETE"
                elif own and all(c.status == "REJECTED" for c in own):
                    hypothesis.status = "INVALID_IMPLEMENTATION"
            next_cycle = int(row.cursor["cycle"]) + 1
            improved = row.cursor.get("recommended", 0) > row.cursor.get("previous_recommended", -1)
            proceed = (
                not row.cursor.get("evidence_only")
                and next_cycle < spec.cycles
                and improved
                and bool(row.cursor.get("recommended", 0))
                and spent.get("quick", 0) < spec.limits.quick
                and (bool(spec.templates) or spent.get("requests", 0) < spec.limits.requests)
            )
            action = "explore_new_family" if proceed else "stop_preserve_budget"
            await self.event(
                session,
                row.id,
                f"next-action:{row.cursor['cycle']}",
                "next_action",
                {
                    "action": action,
                    "taxonomy": dict(taxonomy),
                    "reason": "Continue only on improving evidence within frozen caps."
                    if proceed
                    else (
                        "Caps, weak/unchanged evidence or the requested cycle limit "
                        "stopped further mutations."
                    ),
                    "explorationFraction": spec.exploration_fraction,
                },
            )
            current = await session.get(ResearchCampaign, row.id)
            if current.status != "RUNNING":
                return
            if proceed:
                current.cursor = {
                    "stage": "DISCOVER",
                    "cycle": next_cycle,
                    "previous_recommended": row.cursor.get("recommended", 0),
                }
            else:
                current.status = "COMPLETE"
                current.cursor = current.cursor | {"stage": "COMPLETE"}
                current.message = (
                    "Research completed; recommendations require manual review. No "
                    "alpha was submitted."
                )
                study = await session.get(Study, row.study_id)
                study.status, study.finished_at = StudyStatus.COMPLETE, utcnow()
        if not proceed:
            async with self.db.session() as session:
                study = await session.get(Study, row.study_id)
            await self._ios[row.mode].engine.set_quota(study.task, 0)

    async def utility(self) -> list[dict[str, Any]]:
        """Observed engineering utility; never a predicted alpha return or model promise."""
        async with self.db.session() as session:
            campaigns = list(
                await session.scalars(
                    select(ResearchCampaign).where(ResearchCampaign.mode == "live")
                )
            )
            reservations = list(
                await session.scalars(
                    select(ResearchReservation).where(ResearchReservation.mode == "live")
                )
            )
            candidates = list(
                await session.scalars(
                    select(ResearchCandidate).where(
                        ResearchCandidate.campaign_id.in_([c.id for c in campaigns])
                    )
                )
            )
        results = []
        for model in sorted(
            {str(c.config.get("model") or "declared_templates") for c in campaigns}
        ):
            ids = {
                c.id for c in campaigns if (c.config.get("model") or "declared_templates") == model
            }
            own = [r for r in reservations if r.campaign_id in ids]
            proposals = [c for c in candidates if c.campaign_id in ids]
            attempts = sum(r.amount for r in own if r.meter == "requests")
            valid = sum(c.status != "REJECTED" for c in proposals)
            results.append(
                {
                    "model": model,
                    "campaigns": len(ids),
                    "reservedAttempts": attempts,
                    "validCandidates": valid,
                    "validCandidatesPerAttempt": valid / attempts if attempts else None,
                    "supportedFullReviews": sum(
                        bool(
                            c.evidence
                            and c.evidence.get("recommendation") == "approve_for_manual_review"
                        )
                        for c in proposals
                    ),
                    "reservedMicrousd": sum(r.amount for r in own if r.meter == "microusd"),
                    "knownObservedMicrousd": sum(
                        r.actual or 0 for r in own if r.meter == "microusd"
                    ),
                    "unknownCostOperations": sum(
                        r.actual is None for r in own if r.meter == "microusd"
                    ),
                    "note": (
                        "Recorded engineering yield only. Unknown billing is not zero; "
                        "no automatic model switching."
                    ),
                }
            )
        return results

    async def components(self, spec: CampaignSpec) -> dict[str, Any] | None:
        profile = spec.profile_evidence
        if not spec.component_group or profile is None or not profile.component_regions:
            return None
        async with self.db.session() as session:
            campaigns = list(
                await session.scalars(
                    select(ResearchCampaign).where(ResearchCampaign.mode == spec.mode)
                )
            )
            own = [c for c in campaigns if c.config.get("componentGroup") == spec.component_group]
            approved = list(
                await session.scalars(
                    select(ResearchCandidate).where(
                        ResearchCandidate.campaign_id.in_([c.id for c in own])
                    )
                )
            )
        components = []
        for region in profile.component_regions:
            rows = [c for c in own if c.config.get("region") == region]
            consistent = [
                c
                for c in rows
                if all(
                    c.config.get(k) == spec.model_dump(mode="json").get(k)
                    for k in [
                        "hypothesis",
                        "validationStart",
                        "validationEnd",
                        "embargoDays",
                        "costBps",
                        "profileId",
                    ]
                )
            ]
            ready = (
                len(rows) == len(consistent) == 1
                and rows[0].status == "COMPLETE"
                and any(
                    c.campaign_id == rows[0].id
                    and c.evidence
                    and c.evidence.get("recommendation") == "approve_for_manual_review"
                    and c.decision
                    and c.decision.get("decision") == "approve_for_manual_review"
                    for c in approved
                )
            )
            components.append(
                {
                    "region": region,
                    "campaignIds": [c.id for c in rows],
                    "independentlyReviewed": bool(ready),
                    "reason": None
                    if ready
                    else (
                        "One matching completed campaign with supported Full evidence"
                        " and manual approval is required."
                    ),
                }
            )
        return {
            "group": spec.component_group,
            "components": components,
            "allComponentsReviewed": all(c["independentlyReviewed"] for c in components),
            "submissionReady": False,
            "note": "Regional evidence is never inferred from another component.",
        }

    async def report(self, campaign_id: int, *, complete: bool = False) -> dict[str, Any]:
        row = await self.get(campaign_id)
        async with self.db.session() as session:
            candidates = list(
                await session.scalars(
                    select(ResearchCandidate)
                    .where(ResearchCandidate.campaign_id == campaign_id)
                    .order_by(ResearchCandidate.id)
                    .limit(20000 if complete else 200)
                )
            )
            hypotheses = list(
                await session.scalars(
                    select(ResearchHypothesis).where(ResearchHypothesis.campaign_id == campaign_id)
                )
            )
            events = list(
                await session.scalars(
                    select(ResearchEvent)
                    .where(ResearchEvent.campaign_id == campaign_id)
                    .order_by(ResearchEvent.id)
                    .limit(20000 if complete else 200)
                )
            )
            reservations = list(
                await session.scalars(
                    select(ResearchReservation).where(
                        ResearchReservation.campaign_id == campaign_id
                    )
                )
            )
            trial_ids = [
                identity
                for candidate in candidates
                for identity in [candidate.quick_trial_id, candidate.full_trial_id]
                if identity
            ]
            trials = {
                trial.id: trial
                for trial in await session.scalars(select(Trial).where(Trial.id.in_(trial_ids)))
            }
            counts = {
                str(k): int(v)
                for k, v in (
                    (
                        await session.execute(
                            select(ResearchCandidate.status, func.count())
                            .where(ResearchCandidate.campaign_id == campaign_id)
                            .group_by(ResearchCandidate.status)
                        )
                    ).all()
                )
            }
        meters = {}
        for meter in {r.meter for r in reservations}:
            own = [r for r in reservations if r.meter == meter]
            meters[meter] = {
                "reserved": sum(r.amount for r in own),
                "knownObserved": sum(r.actual or 0 for r in own),
                "unknownOperations": sum(r.actual is None for r in own),
            }

        def trial_data(identity: int | None) -> dict[str, Any] | None:
            trial = trials.get(identity)
            return (
                {
                    "id": trial.id,
                    "number": trial.number,
                    "state": trial.state,
                    "alphaId": trial.alpha_id,
                    "result": trial.result,
                    "settings": trial.settings,
                    "message": trial.message,
                }
                if trial
                else None
            )

        return {
            "components": await self.components(CampaignSpec.model_validate(row.config)),
            "id": row.id,
            "name": row.name,
            "mode": row.mode,
            "status": row.status,
            "config": row.config,
            "configHash": row.config_hash,
            "authorized": row.authorized_hash == row.config_hash,
            "stage": row.cursor.get("stage"),
            "cycle": row.cursor.get("cycle"),
            "message": row.message,
            "retryAt": row.retry_at.isoformat() if row.retry_at else None,
            "createdAt": row.created_at.isoformat(),
            "counts": counts,
            "shortlist": [
                c.id
                for c in candidates
                if c.evidence and c.evidence.get("recommendation") == "approve_for_manual_review"
            ],
            "emptyShortlistReason": "No candidates satisfy recorded Full and robustness evidence."
            if not any(
                c.evidence and c.evidence.get("recommendation") == "approve_for_manual_review"
                for c in candidates
            )
            else None,
            "budgets": meters,
            "hypotheses": [
                {
                    "id": h.id,
                    "datasetId": h.dataset_id,
                    "family": h.family,
                    "status": h.status,
                    "document": h.document,
                }
                for h in hypotheses
            ],
            "candidates": [
                {
                    "id": c.id,
                    "hypothesisId": c.hypothesis_id,
                    "original": c.original,
                    "expression": c.expression,
                    "identity": c.identity,
                    "structure": c.structure,
                    "status": c.status,
                    "reason": c.reason,
                    "provenance": c.provenance,
                    "quick": trial_data(c.quick_trial_id),
                    "full": trial_data(c.full_trial_id),
                    "evidence": c.evidence,
                    "decision": c.decision,
                }
                for c in candidates
            ],
            "events": [
                {
                    "id": event.id,
                    "kind": event.kind,
                    "key": event.key,
                    "at": event.created_at.isoformat(),
                    "payload": event.payload,
                }
                for event in events
            ],
            "approvalQueue": sorted(
                [
                    {"candidateId": c.id, "evidence": c.evidence, "decision": c.decision}
                    for c in candidates
                    if c.evidence
                ],
                key=lambda item: (
                    item["evidence"].get("recommendation") != "approve_for_manual_review",
                    -float(item["evidence"].get("holdoutDelta") or 0),
                    item["candidateId"],
                ),
            ),
        }

    async def decision(self, campaign_id: int, candidate_id: int, decision: str, note: str) -> None:
        async with self.db.session() as session:
            candidate = await session.get(ResearchCandidate, candidate_id)
            if candidate is None or candidate.campaign_id != campaign_id:
                raise ResearchError("Candidate was not found in this campaign.")
            if decision == "approve_for_manual_review" and not (
                candidate.evidence
                and candidate.evidence.get("fullEligibilityObserved")
                and candidate.evidence.get("recommendation") == "approve_for_manual_review"
            ):
                raise ResearchError(
                    "Incomplete Full/robustness evidence cannot enter the approved queue."
                )
            if decision not in {"approve_for_manual_review", "reject"}:
                raise ResearchError("Unknown research review decision.")
            candidate.decision = {
                "decision": decision,
                "note": note,
                "at": utcnow().isoformat(),
                "evidenceHash": hashlib.sha256(
                    json.dumps(candidate.evidence, sort_keys=True).encode()
                ).hexdigest(),
            }

            await self.event(
                session,
                campaign_id,
                f"decision:{candidate_id}:{utcnow().isoformat()}",
                "user_review",
                {"candidateId": candidate_id, **candidate.decision},
            )

    async def refresh_evidence(self, campaign_id: int) -> None:
        row = await self.get(campaign_id)
        if row.status not in {"COMPLETE", "BLOCKED"}:
            raise ResearchError("Finish or pause execution before refreshing cached evidence.")
        async with self.db.session() as session:
            for candidate in await session.scalars(
                select(ResearchCandidate).where(ResearchCandidate.campaign_id == campaign_id)
            ):
                candidate.evidence = None
                candidate.decision = None
            current = await session.get(ResearchCampaign, campaign_id)
            current.status = "RUNNING"
            current.cursor = current.cursor | {
                "stage": "EVALUATE",
                "evidence_version": int(current.cursor.get("evidence_version", 0)) + 1,
                "evidence_only": True,
            }

    async def resolve_uncertain(self, campaign_id: int) -> None:
        row = await self.get(campaign_id)
        if row.status != "BLOCKED" or "Uncertain BRAIN" not in (row.message or ""):
            raise ResearchError("No uncertain execution reservation requires settlement.")
        async with self.db.session() as session:
            await session.execute(
                update(ResearchReservation)
                .where(
                    ResearchReservation.campaign_id == campaign_id,
                    ResearchReservation.meter.in_(["quick", "full"]),
                )
                .values(settled=True)
            )
            stored = await session.get(ResearchCampaign, campaign_id)
            stored.status = "CANCELLED"
            stored.message = (
                "User checked BRAIN and settled concurrency. Spending "
                "reservations are retained; no resend authorized."
            )
            await self.event(
                session, campaign_id, "uncertain-settled", "user_uncertain_settlement", {}
            )
