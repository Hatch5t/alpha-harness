"""Immutable campaign configuration and explicit approval boundaries."""

import hashlib
import json
from datetime import date  # noqa: TC003 — Pydantic resolves this type at runtime
from typing import Literal, Self

from pydantic import Field, model_validator

from ..campaign_review import ResearchDesign
from ..catalog.queries import FieldFilter  # noqa: TC001 — Pydantic schema
from ..labs.template_document import TemplateBody  # noqa: TC001 — Pydantic schema
from ..schemas import Out

SAMPLER = "research-campaign"
type Mode = Literal["offline", "live"]
type Meter = Literal["quick", "full", "requests", "tokens", "microusd"]


class Limits(Out):
    quick: int = Field(default=10, ge=0, le=1000)
    full: int = Field(default=2, ge=0, le=100)
    requests: int = Field(default=2, ge=0, le=100)
    tokens: int = Field(default=100_000, ge=0, le=100_000_000)
    microusd: int = Field(default=0, ge=0, le=1_000_000_000)
    requests_per_minute: int = Field(default=10, ge=1, le=1000)
    concurrent_batches: int = Field(default=1, ge=1, le=8)


class Pricing(Out):
    input_microusd_per_million: int = Field(ge=0, le=1_000_000_000)
    output_microusd_per_million: int = Field(ge=0, le=1_000_000_000)
    source: str = Field(min_length=8, max_length=500)
    verified_on: date


class PromotionWeights(Out):
    fitness: float = Field(default=1, ge=0, le=10, allow_inf_nan=False)
    sharpe: float = Field(default=0.1, ge=0, le=10, allow_inf_nan=False)
    complexity: float = Field(default=0.01, ge=0, le=10, allow_inf_nan=False)
    turnover: float = Field(default=0.1, ge=0, le=10, allow_inf_nan=False)
    novelty: float = Field(default=0.1, ge=0, le=10, allow_inf_nan=False)


class Profile(Out):
    id: str = Field(pattern=r"^[a-z0-9_-]{1,64}$")
    name: str = Field(min_length=3, max_length=128)
    regions: list[str] = Field(default_factory=list, max_length=30)
    constraints: dict[str, str] = Field(default_factory=dict)
    verified: bool = False
    source: str | None = Field(default=None, max_length=1000)
    verified_on: date | None = None
    deadline: date | None = None
    rewards: str | None = Field(default=None, max_length=1000)
    supported: bool = True
    field_filter: FieldFilter | None = None
    max_operators: int = Field(default=32, ge=1, le=32)
    max_fields: int = Field(default=12, ge=1, le=12)
    required_theme: str | None = Field(default=None, min_length=1, max_length=240)
    component_regions: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def evidence_required(self) -> Self:
        if self.verified and (not self.source or not self.verified_on):
            raise ValueError("Verified constraints need a source and verification date.")
        return self


class CampaignSpec(ResearchDesign):
    purpose: Literal["regular", "power_pool"] = "regular"
    name: str = Field(default="Grandmaster Research Campaign #1", min_length=1, max_length=128)
    mode: Mode = "offline"
    profile_id: str = Field(default="general", min_length=1, max_length=64)
    universe: str = Field(default="TOP3000", min_length=1, max_length=64)
    neutralization: str = Field(default="INDUSTRY", min_length=1, max_length=64)
    dataset_ids: list[str] = Field(default_factory=list, max_length=30)
    model: str = Field(default="", max_length=128)
    limits: Limits = Field(default_factory=Limits)
    cycles: int = Field(default=1, ge=1, le=10)
    max_output_tokens: int = Field(default=4096, ge=256, le=16000)
    pricing: Pricing | None = None
    promotion_weights: PromotionWeights = Field(default_factory=PromotionWeights)
    max_discovery_correlation: float = Field(default=0.5, gt=0, le=1, allow_inf_nan=False)
    exploration_fraction: float = Field(default=0.7, ge=0, le=1, allow_inf_nan=False)
    min_observations: int = Field(default=60, ge=60, le=500)
    holdout_declared_untouched: bool = False
    revision_reason: str = Field(default="", max_length=1000)
    templates: list[TemplateBody] = Field(default_factory=list, max_length=20)
    template_source_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    accept_structural_repairs: bool = False
    profile_evidence: Profile | None = None
    component_group: str | None = Field(default=None, min_length=3, max_length=128)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.region == "ALL":
            raise ValueError(
                "Use separate single-region components; ARC cannot bypass region budgets."
            )
        if len(set(self.dataset_ids)) != len(self.dataset_ids):
            raise ValueError("Dataset IDs must be distinct.")
        if self.limits.quick < 1:
            raise ValueError("A campaign needs a positive Quick budget.")
        if self.limits.full > self.limits.quick:
            raise ValueError("Full promotions cannot exceed the Quick candidate budget.")
        if not self.templates and self.limits.requests < 1:
            raise ValueError("Generation needs a provider request or declared templates.")
        return self

    def digest(self) -> str:
        return hashlib.sha256(
            json.dumps(self.model_dump(mode="json"), sort_keys=True).encode()
        ).hexdigest()


class Hypothesis(Out):
    mechanism: str = Field(min_length=12, max_length=2000)
    family: str = Field(min_length=2, max_length=100)
    direction: str = Field(min_length=2, max_length=300)
    horizon: str = Field(min_length=2, max_length=200)
    assumptions: list[str] = Field(min_length=1, max_length=10)
    fields_required: list[str] = Field(min_length=1, max_length=30)
    risk_factors: list[str] = Field(min_length=1, max_length=10)
    failure_regimes: list[str] = Field(min_length=1, max_length=10)
    falsification_tests: list[str] = Field(min_length=1, max_length=10)
    independence: str = Field(min_length=4, max_length=1000)
    estimated_simulations: int = Field(default=10, ge=1, le=100)


class GeneratedAlpha(Out):
    expression: str = Field(min_length=1, max_length=8000)
    hypothesis: int = Field(ge=0, le=5)


class Generated(Out):
    hypotheses: list[Hypothesis] = Field(min_length=1, max_length=6)
    alphas: list[GeneratedAlpha] = Field(min_length=1, max_length=40)

    @model_validator(mode="after")
    def references(self) -> Self:
        if any(alpha.hypothesis >= len(self.hypotheses) for alpha in self.alphas):
            raise ValueError("Each expression must reference a supplied hypothesis.")
        return self


class Authorization(Out):
    config_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    acknowledgement: Literal["AUTHORIZE LIVE RESEARCH WITH THESE EXACT CAPS"]


class GlobalApproval(Out):
    limits: Limits
    acknowledgement: Literal["APPROVE GLOBAL RESEARCH BUDGET"]


class CandidateDecision(Out):
    decision: Literal["approve_for_manual_review", "reject"]
    note: str = Field(min_length=10, max_length=4000)
