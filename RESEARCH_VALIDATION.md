# Autonomous research validation

The release tag identifies the shipped source. GitHub Actions on that commit records the
full gate and each installer job; a preview is published only after every required build
and the native Apple Silicon smoke test passes.

## Local results before release

- 108 backend tests passed, including the original completed Quick-only task review case,
  historical tasks, missing/malformed turnover, request accounting, controlled pilot and the
  new controller fault matrix. Five additional inline mathematical/template self-checks run
  inside this suite.
- Ruff, formatting, pyproject formatting, Pyrefly and strict Basedpyright pass.
- Frontend Biome/TypeScript checks, production build, generated API contract, design lint,
  Rust formatting, constraints export and wheel packaging pass.
- A browser run exercised preview, synthetic execution, pause/resume, completion and a saved
  manual rejection without BRAIN authentication. It completed ten Quick and two Full fixtures.
- A guarded disposable copy of existing app state started and stopped successfully. Nine
  protected tables were unchanged; two model configurations and decryptable saved cookies
  were preserved. The original databases were unchanged. No provider generation or live
  simulation enqueue occurred.
- Private Experiment #1 replay: 40 trials, 37 statically valid, seven narrow repairs, three
  refusals and ten selected with a five/five dataset allocation. Only counts are reported.

## Controller regression coverage

The tests cover reconnecting SQLite at every transition; lost enqueue responses after commit;
lost provider responses after reservation; terminal dispatch identity reuse; paused unsent
records; cancelled campaigns; zero Full and insufficient token budgets; exact authorization
acknowledgements; concurrent last-slot reservations; persistent RPM and global batch leases;
unknown usage; usage overrun stopping; cross-day batch resumption without a lifetime reset;
unsupported/frozen profiles; incomplete ARC components; template grids; missing/constant
correlation; singular covariance; invalid VECTOR options; parameter identity; and positive
Full evidence that remains reviewable rather than submission-ready.

## Native and CI release gates

The release workflow runs the same complete source gate, builds the wheel, records its
SHA-256, and builds Windows x64, Linux amd64, Intel macOS and Apple Silicon macOS installers.
It verifies macOS minimum versions and camera entitlements, then runs the launcher integrity
unit test on Apple Silicon.

The native smoke installs the hashed wheel into disposable state, boots the actual launcher,
checks frontend assets and API health, saves preferences/history and pauses a synthetic
campaign. It quits cleanly, restarts the launcher, verifies persistence, resumes the offline
campaign, checks its ten/two/two caps and downloads its report. Real account credentials and
research databases are never used by this smoke test. Validation branches cannot publish;
the release tag reruns these gates before publishing a prerelease. Prereleases do not change
the stable updater channel.

## Limits of these results

No real alpha quality, paid-provider billing, current BRAIN permission acceptance or online
session validity is asserted from offline tests. Competition rules must be verified before
live execution. Python-alpha execution is unavailable and blocked. Bootstrap/covariance
results are historical diagnostics with stated assumptions, not guaranteed out-of-sample
performance. The preview is ad-hoc signed, not Apple-notarized.

The next milestone is the explicitly approved live canary described in
GRANDMASTER_CONTINUATION.md; it is not part of installer validation and has not been started.
