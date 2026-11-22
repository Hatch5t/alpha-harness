# Autonomous research: audit and implementation gates

Status: a bounded Quick-to-Full foundation, **not a completed autonomous research platform**.
Live simulations, provider calls and submissions were not used to validate this change.

## Audited source

- Fork main: `768e33c46a24238787956b872c318d92e785e151` (rc3 plus developer script mode fix).
- PR #4 baseline: `1b0d5c467ff96ab1a73482a459f67d396fde578a`.
- Original passing CI: run `44991231363`, job `137112512474`; logs confirm 58 backend tests,
  both Python type checkers, lint, frontend checks, API contracts and production build.
  This was not an installer/native smoke run.
- Independent correction: `52eb8d5c28d96727c7d358ece3e78ffe334f9aea`.
- Upstream inspected: `38d296569cf88538210b6a3918af7814f20e1ab1`.
  The histories diverge (15 upstream-only and 48 fork-only commits at the inspected heads).

The audit traced task creation and resizing, persisted sampler parameters, generation and
AST checking, dataset allocation, request/key accounting, queue deduplication, dispatch,
uncertain-send recovery, result harvesting, promotion, review selection, migrations and
release gating. This is not a claim that every unrelated upstream feature was reviewed.

## Findings corrected in PR #4

| Finding | Correction and evidence |
| --- | --- |
| Pilot could inherit Full from global Settings | Persist explicit QUICK on generated and dispatched Quick trials; require Quick permission; no region-agnostic pilot |
| Uncertain BRAIN sends could spend quota twice | Persist `allow_uncertain_retry=false`; recover a matching alpha, otherwise stop after the reconciliation window; backfill existing pilot records, including shared rows |
| Failed provider calls disappeared from daily usage | Charge key/model and all-model daily rows before dispatch; failures and cancellation retain charges |
| Hidden overload retries escaped request accounting | Service owns retries, each with a fresh durable reservation; every transport call disables internal retries |
| Concurrent service instances could read the same remaining request | Conditional SQLite upserts check both model and key ceilings in one transaction; re-read on a lost reservation race |
| Responses across midnight could land on the wrong day | Token usage attaches to the dispatch-day reservation |
| Missing usage looked like zero cost | Show reported tokens plus an explicit unknown-usage indicator; historical rows remain marked incomplete |
| Idle core lending could expand pilot concurrency | One exclusive pilot batch, no borrowing or free rides in other tasks' batches; GLB uses two physical slot units for one batch |
| Paused or unfinished work could be promoted by a direct call | Require RUNNING status, no stopping flag and no open trials |
| Partial enqueue outcomes could orphan trial attribution | Check cardinality before mutating trials; keep strict pairing |

The default promotion rule is generic and deliberately simple: passing Quick checks,
positive finite Fitness and Sharpe, exact expression deduplication, then
`Fitness + 0.1 * Sharpe` with dataset diversity. Each Full row stores its source Quick
number, settings, score and reason. This is not yet the requested configurable research-value
model, economic plausibility assessor or portfolio-aware selection engine.

## Existing architecture to retain

| Capability | Existing implementation | Remaining work |
| --- | --- | --- |
| Catalog discovery | Downloaded DuckDB metadata, typed fields, universe coverage | Durable ranked opportunity backlog and exploration memory |
| Generation | Power Pool prompts, legacy labs, AST parsing and narrow VECTOR repair | Structured hypothesis records, model utility comparison and controlled mutations |
| Execution | SQLite studies/trials, engine deduplication, compare-and-set lifecycle | Campaign/global approvals and durable monetary/token ceilings |
| Pilot | Ten Quick target, two Full maximum, two reserved generation attempts | Broader fault matrix and real platform verification only after approval |
| Validation | Date-aligned PnL/turnover, discovery/validation embargo, rejection reasons | Period-exposure ledger, robustness grading, sensitivity and multiple-testing accounting |
| Portfolio | Existing correlation and submitted-baseline evaluation | Regularized covariance, incremental objectives and unified approval ranking |
| Reviews | Frozen task selection and manual review decisions | One campaign screen and automatic persisted handoffs |
| Competitions | Platform listings plus quarterly campaign view | Source-backed configurable profiles independent of quarter |

Quick checks never prove submission eligibility. Full simulations are also not submissions.
An old history window is not untouched merely because a plan labels it validation.

## Dependency-ordered milestones

1. **Durability gate (this change).** Backend regression/fault tests, API contract, frontend
   build, migration and privacy checks, all installer builds and native ARM launch/restart.
   Keep the PR draft until its actual gate results are known. Do not publish a release from
   this validation branch.
2. **Safe private notebook bridge and upstream reconciliation.** The subsequent data-only
   reader accepts explicitly declared template metadata and reuses Template Lab's existing
   document types. Its CLI ignores all cells/outputs and writes owner-only exports.
   GUI import and migration of dynamically generated notebook recipes remain pending.
   Inspect upstream connection management, engine resilience, wheel hash verification,
   VECTOR explicit options and camera entitlements before selectively adopting changes.
   Gate: hostile-notebook fixtures, type/field validation, migration compatibility and native smoke.
3. **Campaign controller.** Add additive campaign, hypothesis, candidate, transition-event
   and budget-reservation tables. Reuse Study/Trial and the existing engine rather than
   build another simulator. Persist an idempotent handoff before each side effect; ambiguous
   sends consume reservation capacity. Expose one create/preview/pause/resume API and screen.
   Gate: SQLite reconnect at every transition, simultaneous campaigns, no auto budget increase.
4. **Research evidence and private policy.** Durable metadata backlog, mechanism queue,
   failure taxonomy, exposure history, configurable promotion and bounded next-action logic.
   Keep proprietary policies and hypotheses in a local ignored extension. Unknown evidence
   blocks a recommendation; do not manufacture PnL or label reused periods out-of-sample.
   Gate: disjoint dates, missing turnover, malformed responses, zero eligible candidates,
   correlated fixtures and diminishing-return stopping.
5. **Unified approval and profiles.** Frozen submitted baseline, explainable portfolio
   ranking, report export and configurable source-backed competition constraints. Unknown
   rules and expired deadlines must remain explicit. A recommendation never invokes submit.
6. **Complete offline demonstration and release.** Exercise all 17 requested end-to-end
   steps through production orchestration with fake providers and a disposable data directory.
   Only after every release gate passes, create an Apple Silicon preview and rollback guide.
   Mock results establish engineering behavior, not live alpha quality.

## Cost and concurrency limits still to implement

The request ledger is a request ceiling, not a currency or output-token ceiling. RPM is
currently an in-memory throttle. A key's all-model accounting uses model quota dates;
mixed reset-timezone configurations require a separate unambiguous key/global budget clock.
No global campaign spending controller exists yet. No billing or fallback is enabled by
these changes; the pilot pins the chosen model and reserves at most two attempts for life.

Exactly ten completed Quick runs cannot be promised if generation, permissions, data or
infrastructure fail. Ten is the target and hard dispatch budget; failures must stop short
and explain why. An uncertain send is not evidence of a research failure.

## Private research and notebook status

Local AIAC notebooks were found and inspected structurally without execution. The primary
notebook has 27 code cells and no declarative Template Lab import bundle. The safe reader
correctly imports zero templates from it; it does not invent recipes from Python functions.
No notebook cells, expressions, outputs, credentials or provider configuration were copied
into this repository. See `NOTEBOOK_IMPORT.md` for the bridge and remaining integration work.

Store future private extensions outside public source control, for example under the app's
private data directory. A public feature branch and a draft PR do not provide privacy.
Only synthetic expressions belong in public regression fixtures.

## Controlled pilot guide (after explicit live authorization)

1. Keep rc3 installed while the new software is under validation. Do not start a new task
   solely to test an installer against a real account.
2. In Power Pool Lab choose a synced single region/delay and supported universe, available
   datasets and neutralizations, and the already configured model. Use distinct datasets
   when the metadata and two-request budget support it.
3. Enable Controlled research pilot and Only use this model. The target is ten Quick runs,
   at most two Full promotions, at most two reserved LLM attempts and one concurrent batch.
   GLB consumes two platform slots for that batch. The pilot never expands these caps.
4. Preview and inspect problems and prompt coverage. Insufficient metadata or permission
   blocks creation; do not invent dataset names or solve a preview failure by raising caps.
5. Obtain explicit authorization for this concrete live configuration before Run.
6. Monitor Tasks; pause/resume preserves reservations. An interrupted provider attempt may
   use a slot without returning output. Unknown BRAIN sends are reconciled without a second
   send. A zero-pass result produces no Full promotions.
7. Review Full results and frozen research evidence. The separate human approval step does
   not submit an alpha. Submission requires a separate explicit action and authorization.

ChatGPT Plus supplies no programmatic API credit. Provider/model settings and keys remain
local; existing Gemini settings are preserved. Never put keys in templates or reports.

## Migration and rollback

The new columns are additive with server defaults: historical simulation rows retain normal
retry behavior, existing pilot rows become conservative, and old token usage is marked
incomplete rather than guessed. No task database is recreated.

The app's existing startup snapshot runs before migration. Before upgrading, fully quit the
tray app and preserve a coherent backup of the entire data directory (including the master
key and DuckDB state). Do not copy a live SQLite/WAL or DuckDB file piecemeal. Do not restore
anything over a running app.

Rolling back code to rc3 retains the additive columns, but rc3 does **not** enforce the new
pilot recovery policy. Keep all new pilot tasks paused if rolling back. To restore an exact
pre-upgrade state, quit the app, preserve the newer directory separately, then restore the
complete matching backup and rc3 together. Replacing only SQLite discards post-backup task
progress and can mismatch the analytical store; it is not a routine upgrade step.

Signing/notarization and live platform acceptance require separate evidence. The existing
unsigned preview release is not proof that this unfinished autonomous platform is ready.
