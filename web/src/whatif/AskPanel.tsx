import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { STATIC, fmtDate } from "../api";
import { Card } from "../components/ui";
import * as sims from "./api";
import type { Allowance, Refusal } from "./api";
import { usd } from "./describe";

const EXAMPLES = ["What if the buyer accepted a 45-day lead time?", "What if our ISO 9001 certificate were renewed until 2028?",
  "What if the deadline moved back two weeks?"];
const MAX = 2000;

/** "What if…" on the Assessment page: ask the agent a question about this assessment. The answer is a hypothetical
 * simulation on its own page; this assessment never changes. */
export function AskPanel({ lotId, assessmentId, disabled }: { lotId: string; assessmentId: number; disabled?: string }) {
  const navigate = useNavigate();
  const [question, setQuestion] = useState("");
  const [asOf, setAsOf] = useState("");
  const [busy, setBusy] = useState(false);
  const [refused, setRefused] = useState<Refusal | null>(null);
  const [allowance, setAllowance] = useState<Allowance | null>(null);
  const [allowanceError, setAllowanceError] = useState<string | null>(null);

  const loadAllowance = () => {
    if (STATIC) return;
    sims.allowance().then((a) => { setAllowance(a); setAllowanceError(null); })
      .catch((e) => setAllowanceError(sims.refusal(e).message));
  };
  useEffect(loadAllowance, []);

  const submit = async (e?: React.FormEvent) => {
    e?.preventDefault();
    if (busy || !question.trim()) return;
    setBusy(true); setRefused(null);
    try {
      const out = await sims.ask(assessmentId, question.trim(), asOf || undefined);
      navigate(`/lots/${lotId}/simulations/${out.simulation_id}`);
    } catch (err) {
      setRefused(sims.refusal(err));
      loadAllowance();
      setBusy(false);
    }
  };

  const off = STATIC ? "This is a read-only demo, so questions are switched off." : disabled;
  const activeId = allowance?.active?.simulation_id;
  const blocked = !!off || !!activeId || (allowance ? !allowance.can_ask : false);

  return (
    <Card title="What if…" note="A hypothetical investigation. Your assessment stays as it is." className="whatif-panel">
      <p className="small muted" style={{ marginTop: 0, maxWidth: "75ch" }}>
        Ask what would happen if something changed: your evidence, a fact, or what the buyer asks for. An AI agent turns the
        question into hypothetical changes and the same rules re-check a frozen copy of this assessment. It runs in the
        background, usually within a couple of minutes.
      </p>
      {off && <div className="notice" role="note">{off}</div>}
      {activeId && <div className="notice" role="note">
        An investigation is already in progress. You can ask another once it finishes.{" "}
        {/* It may be on another lot; the simulation page moves to the right one. */}
        <Link to={`/lots/${lotId}/simulations/${activeId}`}>Open the investigation in progress</Link>
      </div>}
      {!off && <form onSubmit={submit} aria-label="Ask a what-if question">
        <label className="label" htmlFor="whatif-q">Your question</label>
        <textarea id="whatif-q" className="whatif-input" value={question} maxLength={MAX} disabled={busy || blocked}
          onChange={(e) => setQuestion(e.target.value)} placeholder={EXAMPLES[0]} aria-describedby="whatif-help" />
        <div id="whatif-help" className="small muted">
          For example: {EXAMPLES.map((x, i) => <button type="button" key={i} className="link-btn example" disabled={busy || blocked} onClick={() => setQuestion(x)}>{x}</button>)}
          <span className="count-note">{question.length} / {MAX}</span>
        </div>
        <details className="more">
          <summary>Check as of another date</summary>
          <div className="field" style={{ maxWidth: 240 }}>
            <label htmlFor="whatif-asof">As of</label>
            <input id="whatif-asof" type="date" value={asOf} onChange={(e) => setAsOf(e.target.value)} disabled={busy || blocked} />
            <div className="small muted">Dates such as certificate expiry are judged on this day. Leave empty for today.</div>
          </div>
        </details>
        <div className="actions" style={{ marginTop: 12, alignItems: "center" }}>
          <button className="btn primary" type="submit" disabled={busy || blocked || !question.trim()}>
            {busy ? <><span className="spinner" aria-hidden />Sending…</> : "Ask"}</button>
          <Allowed allowance={allowance} error={allowanceError} />
        </div>
      </form>}
      {refused && <RefusalNote r={refused} lotId={lotId} onRetry={refused.kind === "busy" || refused.kind === "network" || refused.kind === "server" ? () => submit() : undefined} />}
    </Card>
  );
}

function Allowed({ allowance, error }: { allowance: Allowance | null; error: string | null }) {
  if (error) return <span className="small muted" role="status">Your AI allowance could not be loaded: {error}</span>;
  if (!allowance) return null;
  if (!allowance.can_ask && !allowance.active && allowance.next_question_at)
    return <span className="small" role="status">Today's AI allowance is used up. You can ask again from {fmtDate(allowance.next_question_at)}.</span>;
  return <span className="small muted">
    Questions use your AI allowance: {usd(allowance.remaining_usd)} of {usd(allowance.daily_usd)} left in the last 24 hours. One question can use up to {usd(allowance.session_usd)}.
  </span>;
}

export function RefusalNote({ r, lotId, onRetry }: { r: Refusal; lotId: string; onRetry?: () => void }) {
  let text: React.ReactNode = r.message;
  if (r.kind === "active") text = <>You already have an investigation queued or running. Only one runs at a time.{" "}
    {r.simulationId && <Link to={`/lots/${lotId}/simulations/${r.simulationId}`}>Open it</Link>}</>;
  if (r.kind === "allowance") text = <>Today's AI allowance can't cover another question
    {r.remaining !== undefined ? ` (${usd(r.remaining)} left)` : ""}.{r.nextAt ? ` You can ask again from ${fmtDate(r.nextAt)}.` : ""}</>;
  if (r.kind === "busy") text = `TenderFit is busy with other questions right now. Try again in about ${r.retryAfter} seconds.`;
  if (r.kind === "not_found") text = "This assessment could not be found for your supplier. It may have been replaced; reload the page.";
  if (r.kind === "invalid") text = `The question wasn't accepted: ${r.message}`;
  return <div className="error-note whatif-refusal" role="alert">
    {text}
    {onRetry && <button className="btn sm" style={{ marginLeft: 10 }} onClick={onRetry}>Try again</button>}
  </div>;
}
