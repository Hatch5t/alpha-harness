# Development checkpoint

The full autonomous research mandate remains open. No background research or live pilot
is scheduled. rc3 remains the published Apple Silicon preview; do not present build-only
rc4 artifacts as a completed autonomous release.

## Completed commits and validation

- PR #4 correction: `52eb8d5c28d96727c7d358ece3e78ffe334f9aea`.
- PR #4 merge on main: `ca2f4b4cf75693c2f803aca680900e794fa1c6c7`.
- CI: https://github.com/Hatch5t/alpha-harness/actions/runs/44994289463
  passed 72 backend tests, both Python type checkers, lint, formatting, API contracts,
  frontend checks and production build.
- Installer validation: https://github.com/Hatch5t/alpha-harness/actions/runs/44994170812
  passed ARM Mac, Intel Mac, Windows and Linux. Native ARM job `137121756693` confirms
  launcher/frontend/API, clean shutdown and restart persistence. Publish/Pages skipped.
- Private offline replay and guarded startup: 37 valid / 7 repaired / 3 rejected from
  40 trials; selected ten split 5/5. Nine protected tables unchanged, two model configs
  preserved, cached cookies decryptable; zero provider calls and simulation enqueues.
- Next feature branch: `codex/grandmaster-campaign-foundation-v1`, based on the merged
  PR #4 payload. It adds a data-only notebook reader, five tests and shared template
  document types. Local combined suite: 77 tests passed, both Python type checkers passed,
  API contract unchanged. The notebook feature does not create tasks or execute cells.

See the branch's PR and Actions for its exact final commit and CI state. Do not infer
that the prior installer run validates later changes to executable code.

## Reproduction

From `backend/`, using the project's pinned environment:

```sh
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run pyproject-fmt --check pyproject.toml
uv run pyrefly check
uv run basedpyright
PYTHONPATH=src uv run python -m unittest discover -s tests
```

The bounded Phase 2 offline integration test is:

```sh
PYTHONPATH=src uv run python -m unittest discover -s tests -p test_pilot_integration.py
```

It exercises real SQLite, generation validation, dataset allocation, scheduler, engine
queue, harvest, Full lineage, pause/reconnect and repeated ticks with synthetic providers.
It is not the requested 17-step autonomous campaign demonstration.

From `frontend/`: `pnpm install --frozen-lockfile`, `pnpm check`, `pnpm build`.
From the repository root: `sh frontend/tools/codegen/generate.sh --check frontend/src/api/generated.ts`.
Use `GRANDMASTER_ROADMAP.md` for migration/rollback and current pilot instructions;
`NOTEBOOK_IMPORT.md` describes the data-only import boundary.

## Next exact implementation step

First confirm the notebook branch's CI and reconcile upstream infrastructure changes.
Then implement an additive **campaign controller** using the existing simulator:

1. Add persisted `research_campaign`, `research_hypothesis`, `research_candidate`,
   `research_event` and `research_reservation` tables. Separate approved immutable limits
   from consumption and from editable descriptive configuration. Keep proprietary policy
   and hypotheses in the local data directory, with synthetic public fixtures only.
2. Implement a transactional transition/event service with an outbox for handoff to
   Study/Trial. A unique `(campaign, transition, candidate, attempt)` key must prevent
   duplicate dispatch after restart. Reservations cannot be refunded merely because a
   provider response or BRAIN simulation id was lost.
3. Freeze the 10 Quick / up-to-2 Full / 2 provider-attempt / one-batch policy as an
   unexecuted pilot configuration. Add explicit pause/resume/cancel states, blocked
   metadata/auth/budget reasons, and no submission transition.
4. Add fake-provider transition tests before UI work: crash before/after reservation,
   simultaneous campaigns, partial batches, quota reset, malformed generation and zero
   eligible candidates. The controller must stop without expanding budgets.
5. Connect cached metadata discovery and a persisted hypothesis queue. Preserve rationale,
   falsification tests, field provenance, original/repaired expressions and exposure dates.
   Next add robustness and portfolio evaluation, bounded next-action decisions and the
   unified approval queue. The existing campaign review is a component, not this controller.

## Open gates and risks

- No unified campaign start/monitor/review interface or durable multi-cycle controller yet.
- No full hypothesis/discovery backlog, feedback controller or private policy extension.
- No global currency/token ceilings, durable RPM throttle or mixed-timezone key budget policy.
- No complete period-exposure ledger proving untouched holdout data.
- No configurable portfolio-aware promotion scoring or regularized covariance objective.
- No generalized source-backed competition profiles independent of the Q4 view.
- Notebook metadata bridge exists; automatic migration of dynamic Python recipes and GUI
  batch import do not. The inspected notebook yields zero declared templates, correctly.
- Upstream has 15 unique commits at the audited heads; review connection state, resilience,
  wheel verification, vector options and macOS entitlement changes selectively.
- The full 17-step offline demonstration and new autonomous release remain blocked on the
  above work. Real platform acceptance, live quotas and current server-side session validity
  have not been verified. Never infer real alpha quality from mocked results.
- Existing previews are unsigned/unnotarized. Do not release unfinished functionality or
  run any BRAIN simulation, paid model request or alpha submission without explicit approval.
