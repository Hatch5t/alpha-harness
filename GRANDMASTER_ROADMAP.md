# Grandmaster engineering completion

The bounded pilot and notebook bridge from PRs #4 and #5 now have a complete campaign
controller and research workspace. This supersedes the earlier unfinished-platform roadmap.

| Prior remaining work | Implemented behavior |
| --- | --- |
| Campaign orchestration | Persisted phases over existing Studies, Trials, BatchEngine and Vault; dispatch identities survive crashes |
| Global spending | Atomic lifetime and UTC-day reservations, five resource meters, persistent RPM and batch leases, exact live approvals |
| Discovery and feedback | Cached opportunity rankings, structured hypotheses, field/type coverage, failure memory, bounded refinement/exploration and stopping |
| Research review | Date-aligned after-cost evidence, regimes, sensitivity, multiple-testing diagnostic, exposure ledger and portfolio contribution |
| Approval queue | Ranked Full evidence, explicit limitations, evidence-bound manual decisions, export and cached-only refresh |
| Profiles and templates | Frozen source-backed profiles, machine-readable constraints, independent ARC components and data-only notebook UI import |
| Native release | Hash-pinned release wheel, camera entitlement, native pause/restart/resume and offline campaign smoke tests |

The original Quick-only task-freeze regression and historical-task compatibility tests remain
in the full gate. The independent private Experiment #1 replay contains 40 historical trials;
37 pass static validation, seven receive the narrow repair, three are rejected and ten are
allocated evenly across two datasets. No private expressions or research observations are
published.

Selective upstream reconciliation retains the fork's recovery and budgeting changes. VECTOR
required-option handling and macOS camera entitlement are adapted from upstream 38d2965;
wheel-integrity hardening follows the audited c06cae4 design. Unrelated upstream dashboards,
Wasm migration and broad engine refactors are not blindly merged into this release.

Read RESEARCH_CAMPAIGNS.md for usage and the real engineering/scientific limits. The software
never submits alphas, buys API credit, grants itself approval or treats historical performance
as guaranteed future returns. Live acceptance remains an explicitly approved research canary,
not an installer validation step.
