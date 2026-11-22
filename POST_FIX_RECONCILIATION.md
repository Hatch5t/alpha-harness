# Post-fix reconciliation and research gates

PR #8 fixed the proven Gemini HTTP 400 schema-complexity incident. PR #7 is an
overlapping earlier draft; it should not be merged wholesale over that fix.

| PR #7 protection | Disposition |
| --- | --- |
| Compact provider schema, strict local validation | Already in #8; retain dynamically derived schema rather than a second handwritten contract. |
| Safe HTTP diagnostics and one-attempt limits | Already in #8, with broader fault coverage and durable known/unknown usage handling. |
| Temperature/output-token rejection categories | Carried forward as specific allowlisted diagnostics. |
| All-failed generation becomes FAILED | Already protected at generation exit in #8; add the #7 final-stage defense for older interrupted cursors. Mark both campaign and Study FAILED. |
| Terminal campaigns cannot reset requests | Already covered in #8; no budget refunds or replay added. |
| Schema drift assertions | Add required-field/property checks against the Pydantic models. |
| Configured Gemini model integration | Add real SDK/production adapter test with the configured model ID, intercepted by MockTransport. |
| UI terminal cancel behavior | #8 already treats FAILED as terminal and provides an unsent recovery draft. |

The final-stage regression covers a persisted RUNNING/DECIDE cursor with an uncertain
generation and no candidates. Neither stored status nor API status may become COMPLETE.
No private incident artifacts, credentials, expressions or notebook contents are part of
this change.

## Canary boundary

The released campaign configuration requires a positive Quick budget. Its smallest
supported live configuration is one generation attempt, one Quick simulation and zero
Full promotions, one cycle and one concurrent batch. A generation-only live run is not
implemented. Do not describe this alternative as generation-only, silently expand caps,
or simulate by racing a pause request. A strictly zero-simulation canary needs a separate
implementation and review.

Draft creation and plan preview do not authorize or start work. Project-specific billing
tier and active provider rate limits cannot be verified from a local key label or saved
limits. A public free-tier offering does not prove that this key's project qualifies.
Retain unknown historical operations, and leave exhausted global budgets unchanged.

## Research sequence after an approved successful canary

1. Confirm usable structured hypotheses, valid field semantics and retained usage for one
   approved request. A Quick result is preliminary; no Full or submission is implied.
2. Inspect cached availability, freshness, coverage, field types and point-in-time release
   semantics. Separate estimate values from IDs, flags, timestamps and fiscal-period
   codes. VECTOR reducers do not establish alignment between different arrays.
3. Compare a small number of distinct mechanisms: consensus revision, revision breadth,
   persistence and acceleration, with disagreement/liquidity reliability conditioning.
   Require true temporal changes; static rankings and renamed duplicates are separate
   rejection reasons. Examine momentum exposures rather than claiming orthogonality.
4. Import only reviewed declarative templates from notebooks. Parse data/AST; never run
   code cells, shell commands, imports or provider calls during notebook intake. Keep
   private templates and research data outside the public repository.
5. Deduplicate canonical expressions and diversify mechanism families. Freeze the test
   matrix, per-dataset allocation, candidate caps and stopping rules before simulations.
6. Allocate a small Quick budget across valid datasets/families fairly. Review failure
   taxonomy and coverage before increasing volume. A failed transport is not evidence
   against an economic hypothesis.
7. Promote only candidates with meaningful Quick evidence, low redundancy and sufficient
   budget. Full validation requires a separately reviewed promotion allowance.
8. Treat previously examined windows as exploratory. Predeclare untouched future holdouts,
   embargo, transaction costs, subuniverse/regime checks and parameter-sensitivity tests.
   Report multiple testing and selection bias; do not relabel an explored year as OOS.
9. Use the actual submitted portfolio only when its members, dated PnL and turnover are
   available. Otherwise label correlation/incremental-contribution checks unavailable;
   never invent a baseline or fill missing observations silently.
10. Preserve lineage, failed hypotheses and explicit rejection reasons. Put promising
    candidates into manual review, with fresh approval for execution/submission. Verify
    opportunity eligibility, rules, deadlines and rewards from sources before enabling
    competition profiles. No consultant-tier or alpha-performance outcome is guaranteed.
