# Durable autonomous research campaigns

Research Campaigns is the default workspace. It completes the research loop over the
existing catalog, Study/Trial rows, simulator, Vault and LLM service. It never submits an
alpha. Historical tasks, the Q4 review screen and manual labs remain available.

## Start without spending

Open Research Campaigns, leave execution on **Offline demonstration**, preview the plan,
and choose **Create and run offline**. No BRAIN sign-in or provider key is needed.
The default demonstration generates six structured mechanisms across two synthetic datasets,
rejects duplicates and invalid VECTOR usage, allocates five Quick fixtures to each dataset,
promotes at most two to Full fixtures and saves evidence and explicit rejection reasons.
A weak or incomplete fixture can correctly produce an empty shortlist.

Pause, resume, inspect evidence and record a manual rejection. Download private report exports
configuration, lineage, hypotheses, reservations, evidence and decisions. Fixture records are
terminal when created, never enter the live queue or analytical Vault, and are excluded from
startup's real-alpha recapture. Mock results establish software behavior, not alpha quality.

The same production controller can run without the app:

```sh
cd backend
uv run python -m alpha_harness.research.demo --output /tmp/research-demo.json
```

This uses a disposable SQLite database and creates a new owner-only export; it refuses to
replace an existing output file. No private experiment is required or uploaded.

## Execution and recovery

The persisted phases are discovery, structured generation, static validation, fair Quick
allocation, Quick harvest, capped Full promotion, Full harvest, research evidence and a
bounded next-action decision. Hypotheses store mechanism, direction, horizon, assumptions,
fields, risk factors, failure regimes, falsification criteria and independence rationale.
Generation sees cached metadata and prior promising/failed mechanisms. Imported templates
use a bounded Cartesian parameter grid and the existing Template Lab document format.

Exact canonical duplicates are rejected. A structural identity supports allocation diversity;
parameter sensitivity additionally requires unchanged fields/operators and small numeric
changes. VECTOR fields must enter a known reducer directly. A narrow temporal VECTOR repair
requires an explicit campaign option, retains the original expression and makes no claim
of economic equivalence. Required operator options and unknown/repeated keywords are checked.

Every provider operation and simulation batch has a durable reservation. An interrupted
provider request with no saved response consumes its slot and is not replayed. A dispatch
intent is committed with the simulation record, so recovery finds the original record even
if enqueue finished before the Trial link was saved. Payload changes or missing original
records stop safely. A never-sent cancelled record may be resumed using the same identity;
an ambiguous sent record cannot be blindly resent.

An uncertain BRAIN outcome holds the global batch lease. Inspect BRAIN before using the
explicit settlement control. Settlement closes the campaign, retains all spending debits
and grants no resend permission. Pauses and restarts preserve caps and lineage. Native
shutdown cancels workers before closing the engine and databases.

## Caps and live approval

Live campaign execution is disabled until both a global research budget and the exact frozen
campaign configuration are explicitly approved in the app. No such approval is supplied by
the offline demonstration or by installation. A live preflight also requires authentication,
Quick permission, cached legal market settings/operators/fields, supported verified profile
rules, the configured pinned model and recent user-verified maximum token prices. No fallback
model, SDK retry or automatic cap increase is authorized.

Five conservative meters apply: Quick simulations, Full simulations, provider attempts,
tokens and micro USD (1,000,000 micro USD = USD 1). Campaign limits last for the entire
campaign; global limits use a UTC day. RPM and batch leases are persisted. Requests count
before transport begins; missing reported usage stays unknown. Reserved and observed amounts
are separate. Token price estimates use an input byte bound plus framing allowance and an
explicit output limit. Use the highest applicable context/tier prices when approving them;
this is a conservative research reservation ledger, not a provider billing integration.

Resuming a reserved simulation batch on another UTC day debits that day's global allowance
again without resetting or double-charging its campaign lifetime allowance. Previous daily
debits remain in the audit history. Lowered global caps can block an existing reserved batch.

These global controls cover autonomous campaigns. Existing manual labs and chat retain their
own controls. Pause existing lab tasks before live campaign execution. Existing BRAIN platform
quotas remain independent and can stop work before local research caps are exhausted.

## Evidence and approval

Selection uses discovery observations before validation and its embargo. PnL and turnover
are joined by date; incomplete, nonfinite or negative-turnover pairs are excluded explicitly.
No missing value is filled with zero. Missing observations, unknown correlation, missing
baseline or unavailable checks remain reasons to refuse a recommendation.

A saved review includes after-cost validation, doubled-cost stress, regime windows,
independently simulated nearby Quick parameters, cached field coverage, recorded concentration
and subuniverse checks, submitted-baseline contribution and discovery correlation against the
baseline and earlier shortlisted candidates. Power Pool work additionally uses the existing
local submitted-pool correlation calculation. There is no automatic sync or additional
simulation to fill an evidence gap.

A deterministic moving-block bootstrap reports a Bonferroni diagnostic over recorded
expressions. Its local-stationarity assumptions and unrecorded external testing limitation
are explicit. Regularized, diagonal-shrunk covariance estimates use aligned observations and
nonnegative normalized weights. Historical estimates and exploratory/reused windows are not
market forecasts or untouched out-of-sample evidence. A period-exposure ledger records reuse;
an untouched designation additionally requires a prospective plan and explicit declaration.

The queue ranks supported Full evidence before incomplete evidence, then measured portfolio
contribution. Approval records a rationale and evidence hash; it does not submit. Refreshing
cached evidence preserves old audit events, invalidates old decisions and cannot start a new
research cycle. Weak, unchanged or exhausted research stops instead of spending indefinitely.
Infrastructure failures are recorded separately from failed economic hypotheses.

## Profiles and private templates

Profiles cover general, regular, Power Pool, unused fields, AQP, ARC, orthogonal research
and Python-alpha opportunities. Competition rules, deadlines and rewards remain unknown until
a source and verification date are recorded; expired or unsupported profiles are blocked.
Machine-readable field filters, operator/field caps and required themes are enforced. The
profile document is frozen into each campaign. Free-text constraints remain instructions for
human review, not proof of platform eligibility. Python-alpha execution is explicitly blocked
because this FastExpr adapter does not implement that separate interface.

ARC components are separate single-region campaigns with the same componentGroup and frozen
research design. The component report requires one independently completed and manually
reviewed campaign per required region; a missing or duplicate region is not inferred to pass.
All components share the global spending controls.

Notebook import reads only explicit metadata.alpha_harness.template_lab declarations.
Code, outputs and execution hooks are ignored. Preview imported templates before saving them
in Template Lab or adding them to a campaign. Dynamic notebook recipes are not executed.
Keep private hypotheses, templates, expressions, exports and policy settings in local app
storage or an ignored private directory. Public fixtures in this repository are synthetic.

## Upgrade and rollback

Quit the tray app before installing a new DMG. The app retains its existing data location,
BRAIN credential encryption key, sessions, Gemini/model settings, tasks and research history.
Startup takes the existing coherent SQLite snapshot before its additive migration. Do not
replace a live SQLite/WAL or DuckDB file with individual copied files.

New installers pin the release wheel SHA-256 and require dependency hashes. The macOS bundle
includes the camera entitlement and the existing camera usage description. Builds remain
ad-hoc signed previews, without Apple notarization.

If rolling back, first pause all new campaigns and quit the app. Preserve the complete newer
data directory separately; retain the matching key and analytical database. Reinstall the
previous preview without running new campaigns under old code. An exact data rollback needs
a coherent matching whole-directory backup; restoring SQLite alone can discard progress and
mismatch the Vault. Never run two backends against the same data directory.
