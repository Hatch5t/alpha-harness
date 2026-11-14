# Q4 2026 BRAIN Research Campaign

The Q4 Campaign page at `/campaign` tracks quarterly activity and lets you review GLB
D1 Power Pool candidates already stored in Alpha Vault. November 22 is the
planning deadline from the campaign slides. Check signed-in BRAIN for the active
theme, cutoff time and current eligibility.

| Data | Source |
| --- | --- |
| Signals and formulated pyramids | BRAIN quarterly activity. Failed reads display as unavailable on the campaign page. |
| Pyramid priorities | BRAIN multipliers, current-quarter counts and local catalog sync. The rank incorporates market priorities through November 22 and a small work-in-progress preference. |
| Unused field leads | Local market snapshot, zero alpha count, at least 70% instrument coverage and 60% date coverage. Verify current eligibility in BRAIN. |
| Expert planning targets | Provisional slide figures of 20 signals and 10 pyramids. |
| Genius combined performance and official standing | Signed-in BRAIN dashboard. |

## Power Pool research review

1. Sync your GLB D1 catalog and choose a market on the campaign page. Choose USA D1
   explicitly for USA field research.
2. Record the economic mechanism and expected direction. Use Template Lab or Power
   Pool Lab to prepare a small simulation batch. Save the results and daily PnL
   with turnover to the Vault. Use Portfolio to sync submitted baseline alphas.
3. Freeze up to six candidate IDs, up to 24 submitted baseline IDs, the validation
   dates, calendar-day embargo, trading cost and exact theme name or ID.
4. Evaluate the stored evidence after the window closes. Plans saved before the
   validation start are marked prospective. The shortlist is ranked by discovery Sharpe alone.
   Discovery ends before the embargo and validation period. Review after-cost Sharpe,
   common-date equal-weight portfolio delta, exact-expression duplicates, the stored
   BRAIN AMER and theme checks, and local Power Pool correlation. Each Sharpe
   calculation requires at least 60 usable observations.
5. Save a rejection or approve an eligible candidate for manual review. Check current
   BRAIN rules and checks before any manual submission.

The evaluation records one snapshot per experiment. Later downloads do not rewrite
it. The date split shows performance on a user-selected period. Its out-of-sample
value depends on whether the candidate design and selection were independent of
that period. BRAIN's aggregate checks and local Power Pool correlation can include
those dates. The portfolio delta is a local estimate at the specified cost and
weights. The exact-expression screen covers the shortlist and synced submissions,
including syntax-normalized Fast Expressions.

The review calls no LLM, simulation, tagging or submission endpoint. The existing
Labs retain their own provider and budget controls. Cross-region stability automation,
a dedicated Python comparison, AQP descriptions and verified ARC, Osmosis and SPC
tracking remain follow-up work.

## Run on macOS using isolated test data

Use Python 3.14, Node as specified in `frontend/.node-version`, uv and pnpm 12.6.0.
Use an empty data directory for the first run. Only one process can listen on
port 8000, so quit the packaged app before running the backend on that port.

```bash
git clone -b codex/q4-2026-research-command-center \
  https://github.com/Hatch5t/alpha-harness.git alpha-harness-q4
cd alpha-harness-q4/backend
uv sync --frozen
export ALPHA_HARNESS_DATA_DIR="$(mktemp -d)"
uv run uvicorn alpha_harness.main:app --host 127.0.0.1 --port 8000
```

In a second terminal, from the checkout:

```bash
cd frontend
pnpm install --frozen-lockfile
pnpm dev
```

Open `http://localhost:5173/campaign`. To verify account-specific campaign data,
sign in through the local app and sync the relevant markets.

## Validation commands

From the checkout root:

```bash
(cd backend && uv run python -m unittest discover -s tests)
uvx lefthook@2.1.14 run check
uvx lefthook@2.1.14 run build
```

The tests cover the date split, common-date portfolio comparison, partial BRAIN
availability, market selection, additive database setup, and persisted review.
