import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import type { components } from '@/api/generated'
import { errorMessage, http } from '@/api/http'
import { saveText } from '@/lib/download'
import { Button, ErrorNotice, Field, Input, Page, PageHeader, Panel, Textarea } from '@/ui/kit'

type Spec = components['schemas']['CampaignSpec']
type Limits = components['schemas']['Limits']
type Profile = components['schemas']['Profile']
type Template = components['schemas']['TemplateBody']
type Json = Record<string, unknown>
interface Summary {
  id: number
  name: string
  mode: string
  status: string
  stage: string
  message: string | null
}
interface Options {
  modelUtility: Json[]
  profiles: Profile[]
  models: { ref: string; provider: string; id: string }[]
  globalBudgets: Record<string, Limits>
  usageToday: Record<string, Record<string, number>>
  authenticated: boolean
  quickAllowed: boolean
}
interface Plan {
  configHash: string
  problems: string[]
  warnings: string[]
  opportunities: { datasetId: string; score: number; reason: string; fieldCount: number }[]
}
interface Evidence {
  recommendation: string
  reasons: string[]
  synthetic: boolean
  fullEligibilityObserved: boolean
  submissionReady: boolean
  [key: string]: unknown
}
interface Candidate {
  id: number
  expression: string
  original: string
  status: string
  reason: string | null
  hypothesisId: number
  provenance: Json
  quick: Json | null
  full: Json | null
  evidence: Evidence | null
  decision: Json | null
}
interface Report extends Summary {
  storedStatus: string
  generation: {
    allFailed: boolean
    failures: {
      dataset: string | null
      diagnostic: {
        category: string
        message: string
        recovery: string
        httpStatus: number | null
        retryable: boolean
      }
    }[]
  }
  config: Spec
  configHash: string
  dataQuality: { nonfiniteValuesMadeMissing: string[] }
  authorized: boolean
  counts: Record<string, number>
  budgets: Record<string, { reserved: number; knownObserved: number; unknownOperations: number }>
  components?: unknown
  approvalQueue: { candidateId: number }[]
  candidates: Candidate[]
  hypotheses: { id: number; datasetId: string; status: string; document: Json }[]
  events: { id: number; kind: string; at: string; payload: Json }[]
}
const limitDefaults: Limits = {
  quick: 10,
  full: 2,
  requests: 2,
  tokens: 100000,
  microusd: 0,
  requestsPerMinute: 10,
  concurrentBatches: 1,
}
const defaults: Spec = {
  model: '',
  name: 'Grandmaster Research Campaign #1',
  mode: 'offline',
  profileId: 'general',
  hypothesis:
    'Analyst revisions diffuse gradually; test changes against liquidity and stability mechanisms.',
  region: 'GLB',
  delay: 1,
  universe: 'TOP3000',
  neutralization: 'INDUSTRY',
  purpose: 'regular',
  validationStart: '2025-01-01',
  validationEnd: '2025-12-31',
  embargoDays: 5,
  costBps: 5,
  baselineIds: ['synthetic-baseline'],
  limits: {
    quick: 10,
    full: 2,
    requests: 2,
    tokens: 100000,
    microusd: 0,
    requestsPerMinute: 10,
    concurrentBatches: 1,
  },
  cycles: 1,
  acceptStructuralRepairs: false,
  explorationFraction: 0.7,
  holdoutDeclaredUntouched: false,
  maxDiscoveryCorrelation: 0.5,
  maxOutputTokens: 4096,
  minObservations: 60,
  revisionReason: '',
  promotionWeights: { fitness: 1, sharpe: 0.1, complexity: 0.01, turnover: 0.1, novelty: 0.1 },
}
const pretty = (value: unknown) => JSON.stringify(value, null, 2)
const selectStyle = 'h-8 rounded border border-hairline bg-surface-2 px-2 text-body'

export function ResearchScreen() {
  const cache = useQueryClient()
  const [selected, setSelected] = useState<number | null>(null)
  const [draft, setDraft] = useState<Spec>(defaults)
  const [advanced, setAdvanced] = useState('{}')
  const [plan, setPlan] = useState<Plan | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)
  const [ack, setAck] = useState('')
  const [globalText, setGlobalText] = useState('')
  const [globalAck, setGlobalAck] = useState('')
  const [profileText, setProfileText] = useState('')
  const [note, setNote] = useState('')
  const [imported, setImported] = useState<{
    templates: Template[]
    sourceHash: string
    warnings: string[]
  } | null>(null)
  const options = useQuery({
    queryKey: ['research-options'],
    queryFn: () => http.get<Options>('/api/research/options'),
  })
  const history = useQuery({
    queryKey: ['research-history'],
    queryFn: () => http.get<Summary[]>('/api/research'),
    refetchInterval: 3000,
  })
  const report = useQuery({
    queryKey: ['research-report', selected],
    queryFn: () => http.get<Report>(`/api/research/${selected}`),
    enabled: selected !== null,
    refetchInterval: 3000,
  })
  const scopes = useQuery({
    queryKey: ['research-scopes'],
    queryFn: () =>
      http.get<{ region: string; delay: number; universe: string }[]>('/api/catalog/scopes'),
  })
  const markets = [
    ...new Set((scopes.data ?? []).map((s) => `${s.region}/${s.delay}/${s.universe}`)),
  ]
  const data = report.data
  const rankedCandidates = data
    ? [...data.candidates].sort((a, b) => {
        const rank = (candidate: Candidate) => {
          const index = data.approvalQueue.findIndex((item) => item.candidateId === candidate.id)
          return index >= 0 ? index : 1000000 + candidate.id
        }
        return rank(a) - rank(b)
      })
    : []
  async function act(work: () => Promise<unknown>) {
    setBusy(true)
    setError(null)
    try {
      await work()
      await cache.invalidateQueries({ queryKey: ['research-history'] })
      await cache.invalidateQueries({ queryKey: ['research-report'] })
      await cache.invalidateQueries({ queryKey: ['research-options'] })
    } catch (e) {
      setError(e)
    } finally {
      setBusy(false)
    }
  }
  function spec(): Spec {
    const extra: unknown = JSON.parse(advanced)
    if (!extra || typeof extra !== 'object' || Array.isArray(extra))
      throw new Error('Advanced configuration must be an object.')
    return {
      ...draft,
      ...extra,
      ...(imported
        ? { templates: imported.templates, templateSourceHash: imported.sourceHash }
        : {}),
    }
  }
  function update(change: Partial<Spec>) {
    setDraft((old) => ({ ...old, ...change }))
    setPlan(null)
  }
  async function control(action: string) {
    await http.post(`/api/research/${selected}/control`, { action })
  }
  return (
    <Page>
      <PageHeader
        title="Research campaigns"
        description="Define a mechanism and caps, inspect the plan, then run a durable research loop. Full evidence enters a manual review queue."
        actions={
          <Button
            onClick={() => {
              setSelected(null)
              setAck('')
            }}
          >
            New campaign
          </Button>
        }
      />
      {error || options.error || history.error || report.error ? (
        <ErrorNotice error={error || options.error || history.error || report.error} />
      ) : null}
      <div className="flex flex-wrap gap-2">
        {(history.data ?? []).map((row) => (
          <Button
            key={row.id}
            onClick={() => {
              setSelected(row.id)
              setAck('')
            }}
          >
            {row.name} · {row.mode} · {row.status}
          </Button>
        ))}
      </div>
      {selected === null ? (
        <>
          <Panel
            title="New research campaign"
            description="Offline mode uses deterministic fixtures and never calls BRAIN or an LLM. Live mode requires separate, explicit spending approvals."
          >
            <div className="grid gap-4 md:grid-cols-2">
              <Field label="Name">
                <Input
                  value={draft.name ?? ''}
                  onChange={(e) => update({ name: e.target.value })}
                />
              </Field>
              <Field label="Execution">
                <select
                  className={selectStyle}
                  value={draft.mode}
                  onChange={(e) =>
                    update({
                      mode: e.target.value as 'offline' | 'live',
                      baselineIds: e.target.value === 'offline' ? ['synthetic-baseline'] : [],
                    })
                  }
                >
                  <option value="offline">Offline demonstration</option>
                  <option value="live">Live research — explicit approval required</option>
                </select>
              </Field>
              <Field label="Profile">
                <select
                  className={selectStyle}
                  value={draft.profileId}
                  onChange={(e) => update({ profileId: e.target.value })}
                >
                  {(options.data?.profiles ?? []).map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name} ·{' '}
                      {p.supported
                        ? p.verified
                          ? 'verified'
                          : 'rules need verification'
                        : 'unsupported'}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Cached market">
                <select
                  className={selectStyle}
                  value={`${draft.region}/${draft.delay}/${draft.universe}`}
                  onChange={(e) => {
                    const [region, delay, universe] = e.target.value.split('/')
                    update({
                      region: region ?? 'GLB',
                      delay: Number(delay),
                      universe: universe ?? 'TOP3000',
                    })
                  }}
                >
                  <option value="GLB/1/TOP3000">
                    GLB / D1 / TOP3000{' '}
                    {draft.mode === 'offline' ? '(synthetic)' : '(requires cached validation)'}
                  </option>
                  {markets
                    .filter((m) => m !== 'GLB/1/TOP3000')
                    .map((m) => (
                      <option key={m} value={m}>
                        {m}
                      </option>
                    ))}
                </select>
              </Field>
              <Field label="Economic hypothesis" className="md:col-span-2">
                <Textarea
                  value={draft.hypothesis}
                  onChange={(e) => update({ hypothesis: e.target.value })}
                />
              </Field>
              <Field label="Validation start">
                <Input
                  type="date"
                  value={draft.validationStart}
                  onChange={(e) => update({ validationStart: e.target.value })}
                />
              </Field>
              <Field label="Validation end">
                <Input
                  type="date"
                  value={draft.validationEnd}
                  onChange={(e) => update({ validationEnd: e.target.value })}
                />
              </Field>
              <Field
                label="Frozen submitted baseline IDs (comma separated)"
                hint="No baseline means no claim of portfolio improvement."
              >
                <Input
                  value={(draft.baselineIds ?? []).join(', ')}
                  onChange={(e) =>
                    update({
                      baselineIds: e.target.value
                        .split(',')
                        .map((s) => s.trim())
                        .filter(Boolean),
                    })
                  }
                />
              </Field>
              <Field label="Configured model">
                <select
                  className={selectStyle}
                  value={draft.model ?? ''}
                  onChange={(e) => update({ model: e.target.value })}
                >
                  <option value="">
                    {draft.mode === 'offline'
                      ? 'Synthetic generator'
                      : 'Choose an existing configured model'}
                  </option>
                  {options.data?.models.map((m) => (
                    <option key={m.ref} value={m.ref}>
                      {m.ref}
                    </option>
                  ))}
                </select>
              </Field>
            </div>
            <div className="mt-4 grid gap-3 sm:grid-cols-3 lg:grid-cols-6">
              {(
                ['quick', 'full', 'requests', 'tokens', 'microusd', 'requestsPerMinute'] as const
              ).map((key) => (
                <Field key={key} label={key === 'microusd' ? 'Maximum cost (micro USD)' : key}>
                  <Input
                    type="number"
                    min={0}
                    value={draft.limits?.[key] ?? 0}
                    onChange={(e) =>
                      update({
                        limits: {
                          ...limitDefaults,
                          ...draft.limits,
                          [key]: Number(e.target.value),
                        },
                      })
                    }
                  />
                </Field>
              ))}
            </div>
            <details className="mt-4">
              <summary>Advanced research design, pricing, profiles and imported templates</summary>
              <p className="my-2 text-body text-ink-subtle">
                Override fields with JSON: neutralization, datasetIds, purpose, cycles,
                explorationFraction, promotionWeights, expectedTheme, pricing, componentGroup or
                acceptStructuralRepairs. Live LLM generation requires pricing with
                inputMicrousdPerMillion, outputMicrousdPerMillion, source and verifiedOn. Use the
                highest applicable rates; zero is an explicit claim of free pricing.
              </p>
              <Field label="Advanced configuration JSON">
                <Textarea
                  className="min-h-40 font-mono"
                  value={advanced}
                  onChange={(e) => {
                    setAdvanced(e.target.value)
                    setPlan(null)
                  }}
                />
              </Field>
              <Field
                label="Import declarative notebook templates"
                hint="Only explicit template metadata is read. Code and outputs are ignored."
              >
                <input
                  type="file"
                  accept=".ipynb,application/json"
                  onChange={(e) => {
                    const file = e.target.files?.[0]
                    if (file)
                      void act(async () => {
                        if (file.size > 2000000) throw new Error('Notebook exceeds 2 MB.')
                        const result = await http.post<{
                          templates: Template[]
                          sourceHash: string
                          warnings: string[]
                        }>('/api/research/notebook-preview', { text: await file.text() })
                        if (!result.templates.length)
                          throw new Error(
                            'No declarative templates found. Export template_lab metadata first.',
                          )
                        if (result.templates.length > 20)
                          throw new Error('Select at most 20 templates for one campaign.')
                        setImported(result)
                        setPlan(null)
                      })
                  }}
                />
              </Field>
              {imported ? (
                <>
                  <p>
                    {imported.templates.length} declared templates · {imported.warnings.join(' ')}
                  </p>
                  <Button
                    onClick={() => {
                      setImported(null)
                      setPlan(null)
                    }}
                  >
                    Remove imported templates
                  </Button>
                  <Button
                    disabled={busy}
                    onClick={() =>
                      void act(async () => {
                        for (const template of imported.templates)
                          await http.post('/api/template-lab/templates', template)
                      })
                    }
                  >
                    Save imported templates in Template Lab
                  </Button>
                </>
              ) : null}
            </details>
            <div className="mt-4 flex gap-2">
              <Button
                disabled={busy}
                onClick={() =>
                  void act(async () =>
                    setPlan(await http.post<Plan>('/api/research/preview', spec())),
                  )
                }
              >
                Preview plan
              </Button>
              <Button
                disabled={busy || !plan || plan.problems.length > 0}
                onClick={() =>
                  void act(async () => {
                    const frozen = spec()
                    if (
                      (await http.post<Plan>('/api/research/preview', frozen)).configHash !==
                      plan?.configHash
                    )
                      throw new Error('Configuration changed; preview again.')
                    const created = await http.post<{ id: number }>('/api/research', frozen)
                    setSelected(created.id)
                    if (frozen.mode === 'offline')
                      await http.post(`/api/research/${created.id}/control`, { action: 'start' })
                  })
                }
              >
                {draft.mode === 'offline'
                  ? 'Create and run offline'
                  : 'Freeze live plan for approval'}
              </Button>
            </div>
          </Panel>
          {plan ? (
            <Panel title="Plan preview">
              <p>{plan.problems.length ? plan.problems.join(' ') : 'Cached preflight passed.'}</p>
              <p className="text-ink-subtle">{plan.warnings.join(' ')}</p>
              {plan.opportunities.map((o) => (
                <p key={o.datasetId} className="mt-2">
                  <strong>{o.datasetId}</strong> · {o.fieldCount} fields · priority {o.score}
                  <br />
                  {o.reason}
                </p>
              ))}
            </Panel>
          ) : null}
        </>
      ) : data ? (
        <>
          <Panel
            title={`${data.name} · ${data.mode === 'offline' ? 'SYNTHETIC' : 'LIVE'} · ${data.status}`}
            description={`Phase: ${data.stage}. ${data.message ?? ''}`}
            actions={
              <a className="text-accent" href={`/api/research/${data.id}/download`} download>
                Download private report
              </a>
            }
          >
            <div className="flex flex-wrap gap-2">
              {data.status === 'DRAFT' && data.mode === 'offline' ? (
                <Button disabled={busy} onClick={() => void act(() => control('start'))}>
                  Start offline
                </Button>
              ) : null}
              {data.status === 'RUNNING' ? (
                <Button disabled={busy} onClick={() => void act(() => control('pause'))}>
                  Pause
                </Button>
              ) : null}
              {['PAUSED', 'BLOCKED'].includes(data.status) ? (
                <Button
                  disabled={busy || (data.mode === 'live' && !data.authorized)}
                  onClick={() => void act(() => control('resume'))}
                >
                  Resume within frozen caps
                </Button>
              ) : null}
              {!['COMPLETE', 'CANCELLED', 'FAILED'].includes(data.status) ? (
                <Button disabled={busy} onClick={() => void act(() => control('cancel'))}>
                  Cancel remaining work
                </Button>
              ) : null}
              {['COMPLETE', 'BLOCKED'].includes(data.status) ? (
                <Button
                  disabled={busy}
                  onClick={() =>
                    void act(() => http.post(`/api/research/${data.id}/refresh-evidence`))
                  }
                >
                  Refresh cached evidence only
                </Button>
              ) : null}
            </div>
            {data.generation.failures.length ? (
              <div role="alert" className="my-4 space-y-3 text-body">
                <p>
                  {data.generation.allFailed
                    ? 'Generation failed before simulation.'
                    : 'Some generation attempts failed; successful candidates are retained.'}
                </p>
                {data.storedStatus !== data.status ? (
                  <p>
                    Historical stored status: {data.storedStatus}. Display corrected from recorded
                    failures; research records are preserved.
                  </p>
                ) : null}
                {data.generation.failures.map((item, index) => (
                  <div key={`${item.dataset}-${index}`}>
                    <strong>
                      {item.dataset ?? 'Interrupted generation'} · {item.diagnostic.category}
                      {item.diagnostic.httpStatus ? ` · HTTP ${item.diagnostic.httpStatus}` : ''}
                    </strong>
                    <p>
                      {item.diagnostic.message} {item.diagnostic.recovery}
                    </p>
                  </div>
                ))}
                <p>
                  Reserved attempts remain charged. Unknown usage does not mean free. Creating a
                  recovery draft makes no provider calls and requires a new live approval.
                </p>
                <a className="text-accent" href="/ai">
                  Open LLM Integration
                </a>
                <Button
                  disabled={busy}
                  onClick={() => {
                    setDraft({ ...data.config, name: `${data.name} recovery` })
                    setAdvanced('{}')
                    setImported(null)
                    setPlan(null)
                    setAck('')
                    setError(null)
                    setSelected(null)
                  }}
                >
                  Prepare recovery draft
                </Button>
              </div>
            ) : null}
            <table className="my-4 w-full text-left text-body">
              <thead>
                <tr>
                  <th>Meter</th>
                  <th>Reserved</th>
                  <th>Known observed</th>
                  <th>Unknown operations</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(data.budgets).map(([meter, value]) => (
                  <tr key={meter}>
                    <td>{meter}</td>
                    <td>{value.reserved}</td>
                    <td>{value.knownObserved}</td>
                    <td>{value.unknownOperations}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="text-ink-subtle">
              Reservations survive retries, restarts and cancellation. Unknown usage is retained. No
              submission action exists in this loop.
            </p>
            <details className="mt-3">
              <summary>Frozen configuration and approval hash</summary>
              <pre className="overflow-auto text-caption">
                {data.configHash}
                {'\n'}
                {pretty(data.config)}
              </pre>
            </details>
            {data.mode === 'live' && data.status === 'DRAFT' ? (
              <div className="mt-4">
                <p>
                  This authorizes BRAIN simulations and provider requests up to the frozen caps
                  above, subject to the separately approved global daily budget.
                </p>
                <Field label="Type AUTHORIZE LIVE RESEARCH WITH THESE EXACT CAPS">
                  <Input value={ack} onChange={(e) => setAck(e.target.value)} />
                </Field>
                <Button
                  disabled={busy || ack !== 'AUTHORIZE LIVE RESEARCH WITH THESE EXACT CAPS'}
                  onClick={() =>
                    void act(async () => {
                      await http.post(`/api/research/${data.id}/authorize`, {
                        configHash: data.configHash,
                        acknowledgement: ack,
                      })
                      await control('start')
                      setAck('')
                    })
                  }
                >
                  Authorize and start this live campaign
                </Button>
              </div>
            ) : null}
            {data.message?.includes('Uncertain BRAIN') ? (
              <Field label="After inspecting BRAIN, type I CHECKED BRAIN; SETTLE WITHOUT RESENDING">
                <Input value={ack} onChange={(e) => setAck(e.target.value)} />
                <Button
                  disabled={busy || ack !== 'I CHECKED BRAIN; SETTLE WITHOUT RESENDING'}
                  onClick={() =>
                    void act(() =>
                      http.post(`/api/research/${data.id}/settle-uncertain`, {
                        acknowledgement: ack,
                      }),
                    )
                  }
                >
                  Settle concurrency and close campaign
                </Button>
              </Field>
            ) : null}
          </Panel>
          {data.components ? (
            <Panel title="Independent regional components">
              <pre className="overflow-auto whitespace-pre-wrap text-caption">
                {pretty(data.components)}
              </pre>
            </Panel>
          ) : null}
          <Panel title="Hypothesis memory">
            <div className="grid gap-3 md:grid-cols-2">
              {data.hypotheses.map((h) => (
                <details key={h.id}>
                  <summary>
                    {h.datasetId} · {h.status} · hypothesis {h.id}
                  </summary>
                  <pre className="overflow-auto whitespace-pre-wrap text-caption">
                    {pretty(h.document)}
                  </pre>
                </details>
              ))}
            </div>
          </Panel>
          <Panel
            title="Candidate review queue"
            description="Quick results remain preliminary. Review Full evidence, limitations and lineage before approving a candidate for manual consideration."
          >
            <Field label="Decision rationale (at least 10 characters)">
              <Textarea value={note} onChange={(e) => setNote(e.target.value)} />
            </Field>
            {data.candidates.length === 0 ? (
              <p>No candidates yet. Progress and blocked reasons appear above.</p>
            ) : null}
            {rankedCandidates.map((c) => (
              <article className="mt-4 border-t border-hairline pt-3" key={c.id}>
                <p>
                  <strong>Candidate {c.id}</strong> · hypothesis {c.hypothesisId} · {c.status} ·{' '}
                  {c.evidence?.recommendation ?? 'not eligible for approval'}
                </p>
                <code className="block break-words text-body">{c.expression}</code>
                <p className="text-ink-subtle">
                  {c.reason} {c.evidence?.reasons.join(' ')}
                </p>
                <details>
                  <summary>Evidence, original expression, settings and lineage</summary>
                  <pre className="overflow-auto whitespace-pre-wrap text-caption">{pretty(c)}</pre>
                </details>
                {c.decision ? <p>Recorded decision: {String(c.decision['decision'])}</p> : null}
                <div className="mt-2 flex gap-2">
                  <Button
                    disabled={
                      busy ||
                      note.trim().length < 10 ||
                      !c.evidence?.fullEligibilityObserved ||
                      c.evidence.recommendation !== 'approve_for_manual_review'
                    }
                    onClick={() =>
                      void act(() =>
                        http.post(`/api/research/${data.id}/candidates/${c.id}/review`, {
                          decision: 'approve_for_manual_review',
                          note,
                        }),
                      )
                    }
                  >
                    Approve for manual review
                  </Button>
                  <Button
                    disabled={busy || note.trim().length < 10}
                    onClick={() =>
                      void act(() =>
                        http.post(`/api/research/${data.id}/candidates/${c.id}/review`, {
                          decision: 'reject',
                          note,
                        }),
                      )
                    }
                  >
                    Reject
                  </Button>
                </div>
              </article>
            ))}
          </Panel>
          <Panel title="Audit trail and next research action">
            {data.events
              .slice(-20)
              .reverse()
              .map((event) => (
                <details key={event.id}>
                  <summary>
                    {event.at} · {event.kind}
                  </summary>
                  <pre className="overflow-auto whitespace-pre-wrap text-caption">
                    {pretty(event.payload)}
                  </pre>
                </details>
              ))}
          </Panel>
        </>
      ) : (
        <p>Loading campaign…</p>
      )}
      <Panel
        title="Global daily budget and opportunity rules"
        description="Live limits default to zero. These controls apply across autonomous campaigns; existing manual labs retain their own limits."
      >
        <p className="text-body">
          BRAIN session: {options.data?.authenticated ? 'available' : 'unavailable'} · Quick
          permission: {options.data?.quickAllowed ? 'available' : 'unavailable'}
        </p>
        <details className="mt-3">
          <summary>Review and explicitly approve global spending caps</summary>
          <pre className="overflow-auto text-caption">
            {pretty({ caps: options.data?.globalBudgets, usageTodayUTC: options.data?.usageToday })}
          </pre>
          <Field label="Live global budget JSON">
            <Textarea
              className="font-mono"
              placeholder={pretty(options.data?.globalBudgets['live'])}
              value={globalText}
              onChange={(e) => setGlobalText(e.target.value)}
            />
          </Field>
          <Field label="Type APPROVE GLOBAL RESEARCH BUDGET">
            <Input value={globalAck} onChange={(e) => setGlobalAck(e.target.value)} />
          </Field>
          <Button
            disabled={busy || globalAck !== 'APPROVE GLOBAL RESEARCH BUDGET'}
            onClick={() =>
              void act(async () => {
                await http.put('/api/research/global-budget', {
                  limits: JSON.parse(globalText),
                  acknowledgement: globalAck,
                })
                setGlobalAck('')
              })
            }
          >
            Approve global daily caps
          </Button>
        </details>
        <details className="mt-3">
          <summary>Verify or configure an opportunity profile</summary>
          <p className="text-ink-subtle">
            Rules, deadlines and rewards remain unknown until verified with a source and date.
            Unsupported execution interfaces stay blocked. ARC regions are separate campaigns
            sharing a componentGroup and global caps.
          </p>
          <div className="my-2 flex flex-wrap gap-2">
            {options.data?.profiles.map((p) => (
              <Button key={p.id} onClick={() => setProfileText(pretty(p))}>
                {p.name}
              </Button>
            ))}
          </div>
          <Field label="Profile document JSON">
            <Textarea
              className="min-h-48 font-mono"
              value={profileText}
              onChange={(e) => setProfileText(e.target.value)}
            />
          </Field>
          <Button
            disabled={busy || !profileText}
            onClick={() =>
              void act(async () => {
                const p: Profile = JSON.parse(profileText)
                await http.put(`/api/research/profiles/${p.id}`, p)
              })
            }
          >
            Save verified profile document
          </Button>
          <Button
            disabled={!profileText}
            onClick={() => saveText('research-profile.json', profileText, 'application/json')}
          >
            Export profile
          </Button>
        </details>
        <p className="sr-only">{busy ? 'Working' : error ? errorMessage(error) : ''}</p>
      </Panel>
    </Page>
  )
}
