import { useQuery } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { useState } from 'react'
import type { components } from '@/api/generated'
import { http, qs } from '@/api/http'
import { Button, ErrorNotice, Page, PageHeader, Panel } from '@/ui/kit'
import { ResearchReview } from './research-review'

interface Campaign {
  quarter: string
  asOf: string
  active: boolean
  submittedSignals: number | null
  completedPyramids: number | null
  planningSignalsTarget: number
  planningPyramidsTarget: number
  milestones: {
    date: string
    label: string
    source: 'platform_slide' | 'internal_planning' | 'quarter_calendar'
    upcoming: boolean
  }[]
  pyramidCandidates:
    | {
        categoryId: string
        category: string
        region: string
        delay: number
        multiplier: number
        submitted: number
        remaining: number
      }[]
    | null
}

type Scope = components['schemas']['CatalogScopeRow']
type Field = components['schemas']['DataFieldRow']
interface Unused {
  synced: boolean
  syncedAt: string | null
  total: number
  results: Field[]
}

const fmtPct = (value?: number | null) =>
  value == null ? '—' : `${Math.round(value * 100)}%`
const scopeKey = (s: Scope) => `${s.region}/D${s.delay}/${s.universe}`
const pageSize = 30

export function CampaignScreen() {
  const [selected, setSelected] = useState('')
  const [dataset, setDataset] = useState('')
  const [offset, setOffset] = useState(0)
  const campaign = useQuery({
    queryKey: ['campaign', 'summary'],
    queryFn: ({ signal }) => http.get<Campaign>('/api/campaign', signal),
    staleTime: 60_000,
  })
  const scopes = useQuery({
    queryKey: ['campaign', 'scopes'],
    queryFn: ({ signal }) => http.get<Scope[]>('/api/catalog/scopes', signal),
  })
  const available = (scopes.data ?? [])
    .filter((s) => s.instrument_type === 'EQUITY' && s.fields > 0)
    .sort((a, b) => b.fields - a.fields)
  const focus =
    selected === ''
      ? available.find((s) => s.region === 'GLB' && s.delay === 1)
      : available.find((s) => scopeKey(s) === selected)

  const unused = useQuery({
    queryKey: ['campaign', 'unused-fields', focus && scopeKey(focus), dataset, offset],
    enabled: focus !== undefined,
    queryFn: ({ signal }) => {
      if (!focus) throw new Error('Select a synced market.')
      return http.get<Unused>(
        `/api/campaign/unused-fields${qs({
          region: focus.region,
          delay: focus.delay,
          universe: focus.universe,
          dataset: dataset.trim(),
          limit: pageSize,
          offset,
        })}`,
        signal,
      )
    },
  })
  const result = campaign.data

  return (
    <Page>
      <PageHeader title="Q4 Research Campaign" />
      <Panel>
        <div className="flex flex-col gap-3">
          <h2 className="text-title font-semibold">GLB D1 Power Pool</h2>
          <p className="text-body text-ink-muted">
            Planning deadline November 22, 2026. Confirm the active theme, cutoff time, and
            eligibility in signed-in BRAIN. Review the AMER Sharpe check and Power Pool correlation
            before choosing a candidate.
          </p>
          <div className="flex flex-wrap gap-4 text-body">
            <Link to="/competitions" className="text-primary">Competitions</Link>
            <Link to="/labs/template" className="text-primary">Template Lab</Link>
            <Link to="/labs/power-pool" className="text-primary">Power Pool Lab</Link>
            <Link to="/portfolio" className="text-primary">Portfolio</Link>
          </div>
        </div>
      </Panel>
      <ResearchReview />
      {campaign.isError && <ErrorNotice error={campaign.error} title="Campaign details unavailable" />}
      {result && (
        <>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <Panel>
              <h2 className="text-title">Q4 signals</h2>
              <p className="num text-headline">{result.submittedSignals ?? 'Unavailable'}</p>
              <p className="text-body-compact text-ink-muted">
                Provisional Expert planning target {result.planningSignalsTarget}
              </p>
            </Panel>
            <Panel>
              <h2 className="text-title">Q4 formulated pyramids</h2>
              <p className="num text-headline">{result.completedPyramids ?? 'Unavailable'}</p>
              <p className="text-body-compact text-ink-muted">
                Provisional Expert planning target {result.planningPyramidsTarget}
              </p>
            </Panel>
            <Panel>
              <h2 className="text-title">{result.quarter}</h2>
              <p className="text-body text-ink-muted">
                {result.active ? 'Active in BRAIN calendar' : 'Outside the campaign quarter'}
              </p>
              <p className="num text-body-compact">{result.asOf}</p>
            </Panel>
            <Panel>
              <h2 className="text-title">Genius combined performance</h2>
              <p className="text-body text-ink-muted">View in the signed-in BRAIN dashboard.</p>
              <Button onClick={() => void campaign.refetch()}>Refresh campaign</Button>
            </Panel>
          </div>
          <Panel>
            <div className="flex flex-col gap-3">
              <h2 className="text-title font-semibold">Q4 milestones</h2>
              {result.milestones.map((m) => (
                <div key={m.date} className="flex flex-wrap justify-between gap-3 text-body">
                  <span>{m.label}</span>
                  <span className="num text-ink-muted">
                    {m.date} · {m.source === 'platform_slide' ? 'Provisional' :
                      m.source === 'internal_planning' ? 'Research review' : 'Quarter calendar'}
                    {!m.upcoming && ' · Past'}
                  </span>
                </div>
              ))}
            </div>
          </Panel>
          <Panel>
            <div className="flex flex-col gap-3">
              <h2 className="text-title font-semibold">Incomplete synced pyramids</h2>
              <p className="text-body-compact text-ink-muted">
                Local research order based on multiplier, market priorities, and submission counts.
              </p>
              {result.pyramidCandidates === null && <p>Q4 pyramid activity unavailable.</p>}
              {result.pyramidCandidates?.length === 0 && <p>No incomplete synced pyramids.</p>}
              {result.pyramidCandidates?.map((p) => (
                <div key={`${p.categoryId}:${p.region}:${p.delay}`} className="flex flex-wrap gap-3">
                  <span>{p.category} · {p.region}/D{p.delay}</span>
                  <span className="num text-ink-muted">×{p.multiplier} · {p.submitted} submitted · {p.remaining} needed</span>
                </div>
              ))}
              <Link to="/pyramids" className="text-primary">Pyramid matrix</Link>
            </div>
          </Panel>
        </>
      )}
      <Panel>
        <div className="flex flex-col gap-3">
          <h2 className="text-title font-semibold">Unused field leads</h2>
          <p className="text-body-compact text-ink-muted">
            The local snapshot filters for zero alpha count, at least 70% instrument coverage and
            at least 60% date coverage. Verify live unused-field eligibility in BRAIN.
          </p>
          <div className="flex flex-wrap gap-3">
            <label className="flex flex-col gap-1">
              Market
              <select
                className="rounded-md border border-hairline bg-canvas p-2"
                value={focus ? scopeKey(focus) : ''}
                onChange={(e) => {
                  setSelected(e.target.value)
                  setOffset(0)
                }}
              >
                <option value="" disabled>Choose a synced market</option>
                {available.map((s) => (
                  <option key={scopeKey(s)} value={scopeKey(s)}>
                    {scopeKey(s)}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex flex-col gap-1">
              Dataset ID
              <input
                className="rounded-md border border-hairline bg-canvas p-2"
                maxLength={80}
                value={dataset}
                onChange={(e) => {
                  setDataset(e.target.value)
                  setOffset(0)
                }}
              />
            </label>
          </div>
          {scopes.isError && <ErrorNotice error={scopes.error} title="Synced markets unavailable" />}
          {unused.isError && <ErrorNotice error={unused.error} title="Field search unavailable" />}
          {scopes.isSuccess && !focus && <p>Choose a synced market to search for unused fields.</p>}
          {unused.data && !unused.data.synced && <p>This market needs a catalog sync.</p>}
          {unused.data?.synced && (
            <>
              <p className="text-body-compact text-ink-muted">
                {unused.data.total} leads · Last catalog update{' '}
                {unused.data.syncedAt ? new Date(unused.data.syncedAt).toLocaleString() : 'unknown'}
              </p>
              <div className="overflow-x-auto">
                <table className="w-full text-left text-body-compact">
                  <thead className="border-b border-hairline text-ink-subtle">
                    <tr><th>Field</th><th>Dataset</th><th>Type</th><th>Coverage</th><th>Dates</th></tr>
                  </thead>
                  <tbody>
                    {unused.data.results.map((f) => (
                      <tr key={f.field_id} className="border-b border-hairline-subtle">
                        <td className="max-w-lg break-all p-2">
                          {f.field_id}
                          {f.description && <p className="text-ink-muted">{f.description}</p>}
                        </td>
                        <td>{f.dataset_id ?? '—'}</td>
                        <td>{f.field_type ?? '—'}</td>
                        <td className="num">{fmtPct(f.coverage)}</td>
                        <td className="num">{fmtPct(f.date_coverage)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="flex gap-3">
                <Button disabled={offset === 0} onClick={() => setOffset(offset - pageSize)}>Previous</Button>
                <Button
                  disabled={offset + pageSize >= unused.data.total || offset + pageSize > 10000}
                  onClick={() => setOffset(offset + pageSize)}
                >
                  Next
                </Button>
              </div>
            </>
          )}
          <div className="flex flex-wrap gap-4">
            <Link to="/sync" className="text-primary">Sync catalog</Link>
            <Link to="/data" className="text-primary">Data Explorer</Link>
          </div>
        </div>
      </Panel>
    </Page>
  )
}
