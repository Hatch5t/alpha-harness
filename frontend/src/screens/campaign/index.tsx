/** Read-only Q4 campaign command center. No autonomous submission controls. */

import { useQuery } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { http } from '@/api/http'
import { ErrorNotice, Page, PageHeader, Panel } from '@/ui/kit'

interface Priority {
  categoryId: string
  category: string
  region: string
  delay: number
  multiplier: number
  submitted: number
  remaining: number
  priorityScore: number
  isPowerPoolThemeScope: boolean
}

interface Milestone {
  date: string
  label: string
  source: string
  upcoming: boolean
}

interface Campaign {
  quarter: string
  asOf: string
  active: boolean
  submittedSignals: number
  completedPyramids: number
  signalsToExpert: number
  pyramidsToExpert: number
  platformPerformanceAvailable: boolean
  performanceNote: string
  pyramidCandidates: Priority[]
  milestones: Milestone[]
}

interface CatalogScope {
  instrument_type: string
  region: string
  delay: number
  universe: string
  fields: number
}

interface Field {
  field_id: string
  dataset_id: string | null
  category_name: string | null
  description: string | null
  coverage: number | null
  date_coverage: number | null
  alpha_count: number | null
  pyramid_multiplier: number | null
}

interface Unused {
  synced: boolean
  total: number
  results: Field[]
  qualificationNote: string
}

const fmtPct = (value: number | null) => value === null ? '—' : `${Math.round(value * 100)}%`

export function CampaignScreen() {
  const campaign = useQuery({
    queryKey: ['campaign', 'summary'],
    queryFn: () => http.get<Campaign>('/api/campaign'),
    staleTime: 10 * 60 * 1000,
  })
  const scopes = useQuery({
    queryKey: ['catalog', 'scopes'],
    queryFn: () => http.get<CatalogScope[]>('/api/catalog/scopes'),
  })
  // Prefer the current GLB/D1 themed market; otherwise show a downloaded
  // USA/D1 market. No guessed universe: only a synced one can be queried.
  const available = (scopes.data ?? [])
    .filter((s) => s.instrument_type === 'EQUITY' && s.fields > 0)
    .sort((a, b) => b.fields - a.fields)
  const focus = available.find((s) => s.region === 'GLB' && s.delay === 1)
    ?? available.find((s) => s.region === 'USA' && s.delay === 1)
    ?? available[0]

  const unused = useQuery({
    queryKey: ['campaign', 'unused-fields', focus?.region, focus?.delay, focus?.universe],
    enabled: focus !== undefined,
    queryFn: () => http.get<Unused>(
      `/api/campaign/unused-fields?region=${encodeURIComponent(focus!.region)}&delay=${focus!.delay}&universe=${encodeURIComponent(focus!.universe)}&limit=30`
    ),
  })

  const result = campaign.data

  return (
    <Page>
      <PageHeader title="Q4 Research Campaign" />
      <p className="max-w-3xl text-body text-ink-muted">
        Research priorities based on the official BRAIN quarterly counts, synced catalog
        and pyramid multipliers. These are planning aids, not submission guarantees.
      </p>
      {campaign.isError && <ErrorNotice error={campaign.error} title="Campaign metrics unavailable" />}
      {result && (
        <>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
            {[
              { label: 'Quarterly signals', value: String(result.submittedSignals), hint: `${result.signalsToExpert} more toward Expert count` },
              { label: 'Completed pyramids', value: String(result.completedPyramids), hint: `${result.pyramidsToExpert} more toward Expert count` },
              { label: 'Quarter', value: result.quarter, hint: result.active ? 'Active campaign' : 'Outside Q4 2026' },
              { label: 'Genius performance', value: 'Check BRAIN', hint: 'Not inferred from local Sharpe' },
            ].map((entry) => (
              <Panel key={entry.label}>
                <div className="flex flex-col gap-2">
                  <span className="text-caption uppercase text-ink-subtle">{entry.label}</span>
                  <span className="num text-headline text-ink">{entry.value}</span>
                  <span className="text-body-compact text-ink-muted">{entry.hint}</span>
                </div>
              </Panel>
            ))}
          </div>
          <Panel>
            <div className="flex flex-col gap-3">
              <h2 className="text-title font-semibold">Upcoming Q4 milestones</h2>
              {result.milestones.filter((m) => m.upcoming).map((m) => (
                <div key={m.date} className="flex justify-between gap-3 border-b border-hairline pb-2 text-body">
                  <span className="text-ink">{m.label}</span>
                  <span className="num whitespace-nowrap text-ink-muted">{m.date}</span>
                </div>
              ))}
              <p className="text-body-compact text-ink-subtle">
                Dates sourced from the 2026 announcement slides or marked as internal review
                dates. Verify active competition eligibility inside BRAIN.
              </p>
            </div>
          </Panel>
          <Panel>
            <div className="flex flex-col gap-3">
              <h2 className="text-title font-semibold">Research priority · incomplete pyramids</h2>
              <p className="text-body-compact text-ink-subtle">
                Ranked by multiplier, themed scope preference and remaining progress; synced
                categories only. High multiplier does not imply high expected Sharpe.
              </p>
              <div className="overflow-x-auto">
                <table className="w-full text-left text-body-compact">
                  <thead className="border-b border-hairline text-ink-subtle">
                    <tr><th className="p-2">Category</th><th className="p-2">Scope</th><th className="p-2">Multiplier</th><th className="p-2">Progress</th></tr>
                  </thead>
                  <tbody>
                    {result.pyramidCandidates.slice(0, 12).map((p) => (
                      <tr key={`${p.categoryId}:${p.region}:${p.delay}`} className="border-b border-hairline-subtle text-ink-muted">
                        <td className="p-2">{p.category}</td>
                        <td className="num p-2">{p.region}/D{p.delay}</td>
                        <td className="num p-2">×{p.multiplier.toFixed(1)}</td>
                        <td className="num p-2">{p.submitted} submitted · {p.remaining} needed</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {result.pyramidCandidates.length === 0 && <p className="text-ink-subtle">No incomplete synced pyramids were returned.</p>}
              <Link to="/pyramids" className="text-body text-primary">Open pyramid matrix</Link>
            </div>
          </Panel>
          <p className="text-body-compact text-ink-subtle">{result.performanceNote}</p>
        </>
      )}
      <Panel>
        <div className="flex flex-col gap-3">
          <h2 className="text-title font-semibold">Unused data-field discovery</h2>
          {focus ? (
            <p className="text-body-compact text-ink-muted">
              Synced scope: {focus.region}/D{focus.delay}/{focus.universe}. Showing fields with
              Alpha Count = 0, instrument coverage ≥70% and date coverage ≥60%.
            </p>
          ) : (
            <p className="text-body-compact text-ink-muted">
              No downloaded EQUITY market found. Open Sync and download GLB D1 or USA D1 first.
            </p>
          )}
          {unused.isError && <ErrorNotice error={unused.error} title="Field search unavailable" />}
          {unused.data && (
            <>
              <span className="text-body text-ink-muted">{unused.data.total} matching fields in the local snapshot</span>
              <div className="overflow-x-auto">
                <table className="w-full text-left text-body-compact">
                  <thead className="border-b border-hairline text-ink-subtle">
                    <tr><th className="p-2">Field</th><th className="p-2">Dataset</th><th className="p-2">Coverage</th><th className="p-2">History</th></tr>
                  </thead>
                  <tbody>
                    {unused.data.results.slice(0, 20).map((f) => (
                      <tr key={f.field_id} className="border-b border-hairline-subtle text-ink-muted">
                        <td className="max-w-xs break-all p-2 text-ink">{f.field_id}</td>
                        <td className="p-2">{f.dataset_id ?? '—'}</td>
                        <td className="num p-2">{fmtPct(f.coverage)}</td>
                        <td className="num p-2">{fmtPct(f.date_coverage)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="text-body-compact text-ink-subtle">{unused.data.qualificationNote}</p>
            </>
          )}
          <div className="flex flex-wrap gap-4 text-body">
            <Link to="/sync" className="text-primary">Sync catalog</Link>
            <Link to="/data" className="text-primary">Explore data</Link>
            <Link to="/labs/power-pool" className="text-primary">Open Power Pool Lab</Link>
          </div>
        </div>
      </Panel>
    </Page>
  )
}
