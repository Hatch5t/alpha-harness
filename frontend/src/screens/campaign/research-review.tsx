import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { useState } from 'react'
import { http, qs } from '@/api/http'
import { labTasks } from '@/screens/tasks/api'
import { Button, ErrorNotice, Panel } from '@/ui/kit'

interface Candidate {
  alphaId: string
  sharpe: number | null
  fitness: number | null
  turnover: number | null
  discoverySharpe: number | null
  holdoutSharpe: number | null
  holdoutDays: number
  portfolioDays: number
  holdoutDelta: number | null
  poolCorrelation: number | null
  poolVerdict: string | null
  amerCheck: string | null
  themeCheck: string | null
  duplicateIds: string[]
  reasons: string[]
}

type Decision = 'approve_for_manual_review' | 'reject'
interface Experiment {
  id: number
  createdAt: string
  prospective: boolean
  source: {
    taskId: number
    name: string
    totalTrials: number
    uniqueAlphas: number
    selected: number
    rejections: Record<string, number>
    trialFailures: Record<string, number>
    selectionNote: string
  } | null
  plan: {
    hypothesis: string
    candidateIds: string[]
    baselineIds: string[]
    validationStart: string
    validationEnd: string
    embargoDays: number
    costBps: number
    expectedTheme: string | null
    region: string
    delay: number
    purpose: 'power_pool' | 'regular'
  }
  review: {
    candidates: Candidate[]
    decision: { alphaId: string; decision: Decision; note: string } | null
  } | null
}

const BASE = '/api/campaign/experiments'
const INPUT = 'rounded-md border border-hairline bg-canvas p-2 text-body'
const alphaIds = (value: FormDataEntryValue | null) =>
  String(value ?? '')
    .split(/[\s,]+/)
    .filter(Boolean)
const number = (value: number | null) => value?.toFixed(2) ?? '—'

export function ResearchReview() {
  const [before, setBefore] = useState<number>()
  const [fromTask, setFromTask] = useState(false)
  const query = useQueryClient()
  const history = useQuery({
    queryKey: ['campaign', 'experiments', before],
    queryFn: ({ signal }) => http.get<Experiment[]>(`${BASE}${qs({ before })}`, signal),
  })
  const refresh = () => query.invalidateQueries({ queryKey: ['campaign', 'experiments'] })
  const tasks = useQuery({
    queryKey: ['campaign', 'completed-tasks'],
    queryFn: labTasks.list,
    enabled: fromTask,
  })
  const freeze = useMutation({
    mutationFn: (data: FormData) =>
      http.post<Experiment>(fromTask ? `${BASE}/from-task` : BASE, {
        hypothesis: data.get('hypothesis'),
        ...(fromTask
          ? {
              taskId: Number(data.get('task')),
              shortlistSize: Number(data.get('shortlist')),
              maxDiscoveryCorrelation: Number(data.get('correlation')),
            }
          : { candidateIds: alphaIds(data.get('candidates')) }),
        baselineIds: alphaIds(data.get('baseline')),
        validationStart: data.get('start'),
        validationEnd: data.get('end'),
        expectedTheme: String(data.get('theme') ?? '').trim() || null,
        embargoDays: Number(data.get('embargo')),
        costBps: Number(data.get('cost')),
        region: data.get('region'),
        delay: Number(data.get('delay')),
        purpose: data.get('purpose'),
      }),
    onSuccess: () => {
      setBefore(undefined)
      return refresh()
    },
  })
  return (
    <Panel>
      <div className="flex flex-col gap-4">
        <h2 className="text-title font-semibold">Research review</h2>
        <p className="text-body-compact text-ink-muted">
          Save your economic mechanism, shortlist, baseline and validation dates before evaluating
          Vault results. A completed lab task can select its shortlist on discovery data. BRAIN
          checks can already include those dates.
        </p>
        <form
          className="flex flex-col gap-3"
          onSubmit={(e) => {
            e.preventDefault()
            freeze.mutate(new FormData(e.currentTarget))
          }}
        >
          <label className="flex flex-col gap-1">
            Economic hypothesis and expected direction
            <textarea
              className={INPUT}
              name="hypothesis"
              required
              minLength={12}
              maxLength={4000}
            />
          </label>
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="flex flex-col gap-1">
              Candidates from
              <select
                className={INPUT}
                value={fromTask ? 'task' : 'manual'}
                onChange={(e) => setFromTask(e.target.value === 'task')}
              >
                <option value="manual">Alpha IDs</option>
                <option value="task">Completed lab task</option>
              </select>
            </label>
            {fromTask ? (
              <>
                <label className="flex flex-col gap-1">
                  Completed task
                  <select className={INPUT} name="task" required defaultValue="">
                    <option value="" disabled>
                      Choose a task
                    </option>
                    {tasks.data?.tasks
                      .filter((t) => t.status === 'COMPLETE')
                      .map((t) => (
                        <option key={t.id} value={t.id}>
                          {t.id} · {t.name ?? t.templateName ?? t.labName} · {t.region}/D{t.delay}
                        </option>
                      ))}
                  </select>
                </label>
                <label className="flex flex-col gap-1">
                  Shortlist size
                  <input
                    className={INPUT}
                    name="shortlist"
                    type="number"
                    min="1"
                    max="6"
                    defaultValue="3"
                    required
                  />
                </label>
                <label className="flex flex-col gap-1">
                  Maximum absolute discovery correlation
                  <input
                    className={INPUT}
                    name="correlation"
                    type="number"
                    min="0.01"
                    max="1"
                    step="0.01"
                    defaultValue="0.5"
                    required
                  />
                </label>
              </>
            ) : (
              <label className="flex flex-col gap-1">
                Candidate alpha IDs, up to six
                <input className={INPUT} name="candidates" required />
              </label>
            )}
            <label className="flex flex-col gap-1">
              Region
              <input
                className={INPUT}
                name="region"
                defaultValue="GLB"
                pattern="[A-Z]{2,8}"
                required
              />
            </label>
            <label className="flex flex-col gap-1">
              Delay
              <select className={INPUT} name="delay" defaultValue="1">
                <option value="1">D1</option>
                <option value="0">D0</option>
              </select>
            </label>
            <label className="flex flex-col gap-1">
              Research purpose
              <select className={INPUT} name="purpose">
                <option value="power_pool">Power Pool</option>
                <option value="regular">Regular alpha</option>
              </select>
            </label>
            <label className="flex flex-col gap-1">
              Submitted portfolio baseline IDs, up to 24
              <input className={INPUT} name="baseline" />
            </label>
            <label className="flex flex-col gap-1">
              Validation start
              <input className={INPUT} name="start" type="date" required />
            </label>
            <label className="flex flex-col gap-1">
              Validation end
              <input className={INPUT} name="end" type="date" required />
            </label>
            <label className="flex flex-col gap-1">
              Embargo in calendar days
              <input
                className={INPUT}
                name="embargo"
                type="number"
                min="0"
                max="365"
                defaultValue="5"
                required
              />
            </label>
            <label className="flex flex-col gap-1">
              Cost in basis points
              <input
                className={INPUT}
                name="cost"
                type="number"
                min="0"
                max="100"
                step="0.1"
                defaultValue="5"
                required
              />
            </label>
          </div>
          <label className="flex flex-col gap-1">
            Exact active BRAIN theme name or ID
            <input className={INPUT} name="theme" maxLength={240} />
          </label>
          <div>
            <Button type="submit" disabled={freeze.isPending}>
              Freeze research plan
            </Button>
          </div>
          {fromTask && tasks.isError && (
            <ErrorNotice error={tasks.error} title="Tasks unavailable" />
          )}
          <Link to="/tasks" className="text-primary">
            Research tasks and simulations
          </Link>
        </form>
        {freeze.isError && <ErrorNotice error={freeze.error} title="Could not freeze plan" />}
        {history.isError && (
          <ErrorNotice error={history.error} title="Research history unavailable" />
        )}
        {history.data?.map((row) => (
          <ExperimentReview key={row.id} row={row} refresh={refresh} />
        ))}
        <div className="flex gap-3">
          {before !== undefined && <Button onClick={() => setBefore(undefined)}>Latest</Button>}
          {history.data?.length === 30 && (
            <Button onClick={() => setBefore(history.data?.at(-1)?.id)}>Older experiments</Button>
          )}
        </div>
      </div>
    </Panel>
  )
}

function ExperimentReview({ row, refresh }: { row: Experiment; refresh: () => Promise<void> }) {
  const evaluate = useMutation({
    mutationFn: () => http.post(`${BASE}/${row.id}/evaluate`),
    onSuccess: refresh,
  })
  const decide = useMutation({
    mutationFn: (body: { alphaId: string; decision: Decision; note: string }) =>
      http.post(`${BASE}/${row.id}/decision`, body),
    onSuccess: refresh,
  })
  return (
    <div className="flex flex-col gap-2 border-t border-hairline pt-3">
      <h3 className="text-title">
        Experiment {row.id} · {row.prospective ? 'Prospective validation' : 'Historical validation'}
      </h3>
      <p className="whitespace-pre-wrap text-body">{row.plan.hypothesis}</p>
      <p className="num text-body-compact text-ink-muted">
        {row.plan.candidateIds.join(', ')} · {row.plan.validationStart} to {row.plan.validationEnd}
        {' · '}
        {row.plan.embargoDays} day embargo · {row.plan.costBps} bps
      </p>
      <p className="text-body-compact text-ink-muted">
        {row.plan.region}/D{row.plan.delay}
        {' · '}
        {row.plan.purpose === 'power_pool' ? 'Power Pool' : 'Regular'} · Baseline{' '}
        {row.plan.baselineIds.join(', ') || 'unavailable'} · Theme{' '}
        {row.plan.expectedTheme ?? 'unverified'}
      </p>
      {row.source && <TaskSelection row={row} />}
      <a href={`${BASE}/${row.id}/export`} download className="text-primary">
        Export experiment history
      </a>
      {!row.plan.candidateIds.length && <p>No candidate survived discovery selection.</p>}
      {!row.review && row.plan.candidateIds.length > 0 && (
        <div>
          <Button disabled={evaluate.isPending} onClick={() => evaluate.mutate()}>
            Evaluate stored evidence
          </Button>
        </div>
      )}
      {evaluate.isError && <ErrorNotice error={evaluate.error} title="Evaluation failed" />}
      {row.review?.candidates.map((c) => (
        <div key={c.alphaId} className="flex flex-col gap-2 rounded-md border border-hairline p-3">
          <Link to="/alpha/$alphaId" params={{ alphaId: c.alphaId }} className="text-primary">
            {c.alphaId}
          </Link>
          <p className="num text-body-compact">
            Discovery Sharpe {number(c.discoverySharpe)} · Validation Sharpe{' '}
            {number(c.holdoutSharpe)}
            {' · '}
            {c.holdoutDays} dates · Portfolio ΔSharpe {number(c.holdoutDelta)}
            {' · '}
            {c.portfolioDays} common dates
          </p>
          <p className="num text-body-compact">
            BRAIN Sharpe {number(c.sharpe)} · Fitness {number(c.fitness)} · Turnover{' '}
            {number(c.turnover)}
            {' · '}Local pool correlation {number(c.poolCorrelation)} ({c.poolVerdict ?? 'unknown'})
            {' · '}AMER {c.amerCheck ?? 'unknown'} · Theme {c.themeCheck ?? 'unknown'}
          </p>
          {c.duplicateIds.length > 0 && <p>Matching IDs {c.duplicateIds.join(', ')}</p>}
          {c.reasons.length > 0 && (
            <ul className="list-disc pl-5 text-body-compact text-ink-muted">
              {c.reasons.map((reason) => (
                <li key={reason}>{reason}</li>
              ))}
            </ul>
          )}
        </div>
      ))}
      {row.review?.decision && (
        <p className="text-body">
          {row.review.decision.decision === 'reject' ? 'Rejected' : 'Approved for manual review'}
          {' · '}
          {row.review.decision.alphaId} · {row.review.decision.note}
        </p>
      )}
      {row.review && !row.review.decision && (
        <form
          className="flex flex-wrap items-end gap-2"
          onSubmit={(e) => {
            e.preventDefault()
            const data = new FormData(e.currentTarget)
            decide.mutate({
              alphaId: String(data.get('alpha')),
              decision: data.get('decision') === 'reject' ? 'reject' : 'approve_for_manual_review',
              note: String(data.get('note')),
            })
          }}
        >
          <label className="flex flex-col gap-1">
            Alpha
            <select className={INPUT} name="alpha">
              {row.review.candidates.map((c) => (
                <option key={c.alphaId}>{c.alphaId}</option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1">
            Decision
            <select className={INPUT} name="decision">
              <option value="reject">Reject</option>
              <option value="approve_for_manual_review">Approve for manual review</option>
            </select>
          </label>
          <label className="flex flex-col gap-1">
            Reason
            <input className={INPUT} name="note" required minLength={10} maxLength={4000} />
          </label>
          <Button type="submit" disabled={decide.isPending}>
            Save decision
          </Button>
        </form>
      )}
      {decide.isError && <ErrorNotice error={decide.error} title="Decision was not recorded" />}
    </div>
  )
}

interface SelectionPage {
  total: number
  results: {
    alphaId: string
    discoverySharpe: number | null
    selected: boolean
    reason: string | null
    discoveryCorrelation: number | null
  }[]
}

function TaskSelection({ row }: { row: Experiment }) {
  const [opened, setOpened] = useState(false)
  const [offset, setOffset] = useState(0)
  const selection = useQuery({
    queryKey: ['campaign', 'selection', row.id, offset],
    queryFn: ({ signal }) =>
      http.get<SelectionPage>(`${BASE}/${row.id}/selection${qs({ offset })}`, signal),
    enabled: opened,
  })
  if (!row.source) return null
  return (
    <div className="flex flex-col gap-2">
      <Link
        to="/tasks/$taskId"
        params={{ taskId: String(row.source.taskId) }}
        className="text-primary"
      >
        {row.source.name}
      </Link>
      <p className="num text-body-compact">
        {row.source.totalTrials} trials · {row.source.uniqueAlphas} distinct alphas ·{' '}
        {row.source.selected} selected
      </p>
      <p className="text-body-compact text-ink-muted">{row.source.selectionNote}</p>
      {Object.entries(row.source.rejections).map(([reason, count]) => (
        <p key={reason}>
          {count} · {reason}
        </p>
      ))}
      {Object.entries(row.source.trialFailures).map(([reason, count]) => (
        <p key={reason}>
          {count} failed trials · {reason}
        </p>
      ))}
      <div>
        <Button onClick={() => setOpened(!opened)}>
          {opened ? 'Hide selection history' : 'Selection history'}
        </Button>
      </div>
      {opened && selection.isError && (
        <ErrorNotice error={selection.error} title="Selection history unavailable" />
      )}
      {opened && selection.data && (
        <>
          <div className="overflow-x-auto">
            <table className="w-full text-left text-body-compact">
              <thead>
                <tr>
                  <th>Alpha</th>
                  <th>Discovery Sharpe</th>
                  <th>Discovery correlation</th>
                  <th>Selection</th>
                </tr>
              </thead>
              <tbody>
                {selection.data.results.map((item) => (
                  <tr key={item.alphaId}>
                    <td>{item.alphaId}</td>
                    <td className="num">{number(item.discoverySharpe)}</td>
                    <td className="num">{number(item.discoveryCorrelation)}</td>
                    <td>{item.selected ? 'Selected' : item.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="flex gap-2">
            <Button disabled={offset === 0} onClick={() => setOffset(offset - 30)}>
              Previous
            </Button>
            <Button
              disabled={offset + 30 >= selection.data.total}
              onClick={() => setOffset(offset + 30)}
            >
              Next
            </Button>
          </div>
        </>
      )}
    </div>
  )
}
