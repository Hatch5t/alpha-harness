# Q4 2026 BRAIN Research Campaign — Alpha Harness preview

This branch adds a **read-only** Q4 campaign dashboard at `/campaign`, built on the
existing local Alpha Harness APIs and DuckDB catalog.

It does **not** automatically submit, tag, or simulate an Alpha; it cannot infer
official Genius combined performance from local PnL. Official BRAIN theme
eligibility, AQP awards, rank and performance must be checked on BRAIN.

## Where the values come from

| Figure | Source |
| --- | --- |
| Submitted signals this quarter | `/api/quarter` — BRAIN submission activity |
| Formulated pyramids | `/api/quarter` — BRAIN pyramid submissions |
| Pyramid priority list | `/api/catalog/pyramids` — platform multiplier/count + local sync state |
| Unused-field candidates | Synced local DuckDB field snapshot, filtered to `alpha_count = 0` |
| Combined Genius/Osmosis scores | **Not available**; check signed-in BRAIN dashboard |
| Q4 dates | Calendar quarter Oct 1–Dec 31, 2026 |
| Nov 9 / Nov 22 dates | Screenshots of Q4 2026 BRAIN opportunities presentation |
| Nov 30 review | Internal research planning marker, **not** an official AQP deadline |

Current experimental Expert planning thresholds (20 signals, 10 pyramids,
0.5 combined performance) reflect the Q4 2026 dashboard screenshots. Counts
come live from BRAIN; performance is deliberately not fabricated.

A completed pyramid requires at least three submitted alphas in a region,
delay, and dataset category, as coded in the existing BRAIN platform module.

The priority score **is not an expected return**. It ranks available,
not-yet-formulated pyramids by multiplier plus a small preference for the
time-sensitive GLB D1/USA markets and existing work in progress.
A high multiplier cannot rescue a poor, correlated or ineligible alpha.

## Run the development version on macOS

Do **not** keep the packaged Alpha Harness app running while developing.
Two instances can collide over the backend's port 8000 and DuckDB locks.
Back up the local Alpha Harness data directory before opening a newer build.
Never commit the data directory or private expressions to this public fork.

```bash
# One-time prerequisites: git, Node.js 24+, uv and pnpm.
# If pnpm is absent:
npm install -g pnpm@12.6.0

git clone -b codex/q4-2026-research-command-center \
  https://github.com/Hatch5t/alpha-harness.git ~/Developer/alpha-harness-q4
cd ~/Developer/alpha-harness-q4/backend
uv sync --frozen
uv run uvicorn alpha_harness.main:app --reload --port 8000
```

In **another Terminal tab**:

```bash
cd ~/Developer/alpha-harness-q4/frontend
pnpm install --frozen-lockfile
pnpm dev
```

Open the URL printed by Vite (normally `http://localhost:5173/campaign`).
The backend listens only on `127.0.0.1:8000`. Ensure the frontend's Vite
proxy can reach the backend. Sign in to BRAIN only via the local Alpha
Harness interface.

## First session

1. Confirm the current quarter in the dashboard and the official BRAIN
   Genius site; investigate discrepancies before research decisions.
2. In **Sync**, sync GLB D1 and USA D1 market data. The campaign screen
   picks the largest available synced GLB D1 universe, falling back to USA D1.
3. In **Q4 Campaign**, review incomplete pyramids, their multipliers and
   the zero-count field candidates. Alpha Count can change after the last sync.
4. In **Data Explorer**, inspect an individual field's type, dataset,
   instrument/date coverage, region availability and meaning.
5. In **Power Pool Lab**, run small, controlled research batches only after
   inspecting the proposed prompt, settings, legal operators and API budget.
6. In **Portfolio**, examine incremental Sharpe, correlations, turnover and
   out-of-sample stability. AQP descriptions require substantive,
   evidence-grounded economic hypotheses, not invented narratives.

## AI cost and research safeguards

The app has provider/key/model/budget controls under **LLM Integration**.
Choose an actually available low-cost or free model from the provider list,
set explicit daily caps and test one request before automating a batch.
ChatGPT Plus is separate from API billing; no personal ChatGPT credentials
can be automatically transferred to Alpha Harness.

For now, **submissions, tagging, approval of external API charges, and
credential handling stay manual**. Future automation can add
hypothesis generation, candidate lineage, validation gates, duplicate
detection and an approval queue once this baseline is verified.

Do not commit API keys, raw BRAIN responses, alpha expressions, session
cookies, private performance histories or database files to this repository.

## Validation

From the `backend` directory:

```bash
uv run python -m unittest discover -s tests
```

The existing CI gate also performs formatting/type checks and frontend
TypeScript/build checks. No live BRAIN simulation or submission is needed
for the campaign's pure unit tests.
