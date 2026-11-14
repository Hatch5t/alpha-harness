import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { useState } from 'react'
import { http, qs } from '@/api/http'
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
  plan: {
    hypothesis: string
    candidateIds: string[]
    baselineIds: string[]
    validationStart: string
    validationEnd: string
    embargoDays: number
    costBps: number
    expectedTheme: string | null
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
  const query = useQueryClient()
  const history = useQuery({
    queryKey: ['campaign', 'experiments', before],
    queryFn: ({ signal }) => http.get<Experiment[]>(`${BASE}${qs({ before })}`, signal),
  })
  const refresh = () => query.invalidateQueries({ queryKey: ['campaign', 'experiments'] })
  const freeze = useMutation({
    mutationFn: (data: FormData) =>
      http.post<Experiment>(BASE, {
        hypothesis: data.get('hypothesis'),
        candidateIds: alphaIds(data.get('candidates')),
        baselineIds: alphaIds(data.get('baseline')),
        validationStart: data.get('start'),
        validationEnd: data.get('end'),
        expectedTheme: String(data.get('theme') ?? '').trim() || null,
        embargoDays: Number(data.get('embargo')),
        costBps: Number(data.get('cost')),
      }),
    onSuccess: () => {
      setBefore(undefined)
      return refresh()
    },
  })
  return (
    <Panel>
      <div className="flex flex-col gap-4">
        <h2 className="text-title font-semibold">Power Pool research review</h2>
        <p className="text-body-compact text-ink-muted">
          Save your economic mechanism, shortlist, baseline and validation dates before evaluating
          Vault results. Use an untouched validation window. Full-period BRAIN checks can already
          include those dates.
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
              Candidate alpha IDs, up to six
              <input className={INPUT} name="candidates" required />
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
        Baseline {row.plan.baselineIds.join(', ') || 'unavailable'} · Theme{' '}
        {row.plan.expectedTheme ?? 'unverified'}
      </p>
      {!row.review && (
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
