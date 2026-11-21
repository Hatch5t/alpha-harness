# Q4 2026 BRAIN Research Campaign

The Q4 Campaign page at `/campaign` tracks quarterly activity and lets you review
Power Pool and regular-alpha candidates stored in Alpha Vault, for the selected
region and delay. November 22 is the
planning deadline from the campaign slides. Check signed-in BRAIN for the active
theme, cutoff time and current eligibility.

| Data | Source |
| --- | --- |
| Signals and formulated pyramids | BRAIN quarterly activity. Failed reads display as unavailable on the campaign page. |
| Pyramid priorities | BRAIN multipliers, current-quarter counts and local catalog sync. The rank incorporates market priorities through November 22 and a small work-in-progress preference. |
| Unused field leads | Local market snapshot, zero alpha count, at least 70% instrument coverage and 60% date coverage. Verify current eligibility in BRAIN. |
| Expert planning targets | Provisional slide figures of 20 signals and 10 pyramids. |
| Genius combined performance and official standing | Signed-in BRAIN dashboard. |

## Research review

1. Sync your GLB D1 catalog and choose a market on the campaign page. Choose USA D1
   explicitly for USA field research.
2. Record the economic mechanism and expected direction. Use Template Lab or Power
   Pool Lab to prepare a small simulation batch. Save the results and daily PnL
   with turnover to the Vault. Use Portfolio to sync submitted baseline alphas.
3. Enter candidate IDs or choose a completed lab task. Set the research purpose,
   region and delay. Freeze the validation dates, calendar-day embargo, cost, up to
   24 submitted baseline IDs and, when relevant, the exact BRAIN theme. Task selection
   keeps up to six candidates, ranked by discovery Sharpe. Its default absolute
   correlation ceiling is 0.5, following the existing Evolution Lab. It requires
   250 common discovery observations to establish pairwise diversification. These
   are local research settings.
4. Evaluate the stored evidence after the window closes. Plans saved before the
   validation start are marked prospective. The shortlist is ranked by discovery Sharpe alone.
   Discovery ends before the embargo and validation period. Review after-cost Sharpe,
   common-date equal-weight portfolio delta, exact-expression duplicates, and
   applicable stored BRAIN checks. GLB D1 retains its AMER check. Power Pool
   reviews include exact-theme matching and local pool correlation. Regular
   reviews use the selected market and its stored submission checks. Each Sharpe
   calculation requires at least 60 usable observations.
5. Save a rejection or approve an eligible candidate for manual review. Check current
   BRAIN rules and checks before any manual submission.

Task selection records successful, failed and repeated trials, per-alpha selection
reasons, its configuration, and the shortlist. A missing Vault metadata or PnL row
asks for a sync before selection. An empty shortlist still produces a rejection
report. Retrying the same task and research design returns the saved experiment.
Use Selection history to page through results and Export experiment history to
download the inputs, trial expressions, settings, selection and review as JSON.
The stored history survives deletion of its original task. PnL is read in batches
of 200 alphas, and only up to six selected series are kept for diversification.

The evaluation records one snapshot per experiment. Later downloads do not rewrite
it. The date split shows performance on a user-selected period. Its out-of-sample
value depends on whether the candidate design and selection were independent of
that period. BRAIN's aggregate checks and local Power Pool correlation can include
those dates. The portfolio delta is a local estimate at the specified cost and
weights. The exact-expression screen covers the shortlist and synced submissions,
including syntax-normalized Fast Expressions.

Run simulations through the existing Labs and Tasks controls. Creating or evaluating
a research review reads local results. It calls no LLM, simulation, tagging or
submission endpoint. The existing
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

The tests cover discovery and validation isolation, positive and negative correlation
filters, incomplete pair measurements, empty shortlists, retries, old database
migrations, export after task deletion, partial BRAIN availability, market-specific
checks, common-date portfolio comparison and persisted human review.
