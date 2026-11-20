import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { usePageTitle } from "../App";
import { STATIC, fmtDate } from "../api";
import { Card, Empty, ErrorNote, Skeleton, Tag, Tile } from "../components/ui";
import * as sims from "./api";
import type { Outcome, ResultRow, Simulation, Status, Step, Summary } from "./api";
import { INPUT, type Phase, describeChange, describeStep, phaseOf, usd } from "./describe";
import { Provenance } from "./Provenance";
import { useInvestigation } from "./useInvestigation";

const LABEL: Record<Status, [string, string, string]> = {
  met: ["✓", "Met", "ok"], not_met: ["✕", "Not met", "bad"], needs_confirmation: ["◷", "Needs confirmation", "warn"], unknown: ["?", "Unknown", "outline"],
};

/** A verdict inside a simulation. Never the plain verdict pill: a dashed, labelled chip, so a hypothetical verdict
 * cannot be mistaken for one from the real assessment. */
export function SimVerdict({ status, real }: { status: Status | null; real?: boolean }) {
  if (!status) return <span className="muted small">not decided</span>;
  const [icon, label, tone] = LABEL[status] ?? ["·", status, "muted"];
  return <span className={`pill sm ${tone} ${real ? "" : "hypo"}`}><span className="i" aria-hidden>{icon}</span>{label}
    {!real && <span className="sr-only"> (hypothetical)</span>}</span>;
}

export const HYPOTHETICAL = "Hypothetical. Your assessment, your profile and the tender are unchanged. This is not a verdict.";

export function HypotheticalBanner() {
  return <div className="banner hypo-banner" role="note" aria-label="Hypothetical simulation">
    <span className="b-icon" aria-hidden>?</span>
    <div className="body"><strong>{HYPOTHETICAL}</strong>
      <p>A what-if simulation re-checks a frozen copy of your assessment with hypothetical changes. Changes to your profile are made on the Profile page.</p></div>
  </div>;
}

export default function SimulationPage() {
  const { lotId, simId } = useParams();
  const id = Number(simId);
  const navigate = useNavigate();
  const inv = useInvestigation(id);
  const { sim, trace, job, results } = inv;
  const [discarding, setDiscarding] = useState(false);
  const [running, setRunning] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  usePageTitle(sim ? `What if: ${sim.title ?? `simulation ${sim.id}`}` : "What if");

  // The URL's lot is only for navigation; the simulation says which lot it belongs to.
  useEffect(() => {
    if (sim && String(sim.lot_id) !== lotId) navigate(`/lots/${sim.lot_id}/simulations/${sim.id}`, { replace: true });
  }, [sim?.lot_id, lotId]);

  const statements = useMemo(() => {
    const m = new Map<number, string>();
    for (const r of results ?? []) m.set(r.requirement_id, r.statement);
    for (const c of trace?.outcome?.changed ?? []) m.set(c.requirement_id, c.statement);
    return m;
  }, [results, trace]);
  const statement = (rid: number) => statements.get(rid);

  if (!Number.isFinite(id)) return <Empty title="Simulation not found" />;
  if (inv.loading && !sim) return <><HypotheticalBanner /><Card><Skeleton lines={5} /></Card></>;
  if (!sim) return <>
    <div className="crumb"><Link to={`/lots/${lotId}`}>Back to the assessment</Link></div>
    <HypotheticalBanner />
    <ErrorNote error={inv.error === "simulation not found" ? "This simulation could not be found for your supplier." : inv.error} />
  </>;

  const phase = phaseOf(sim, trace, job);
  const question = trace?.steps.find((s) => s.tool === "question")?.input.question as string | undefined;
  const canDiscard = !STATIC && (sim.status === "created" || sim.status === "completed" || sim.status === "failed");
  const discard = async () => {
    if (!window.confirm("Discard this simulation? It is kept in the record but hidden from the list. Your assessment is not affected.")) return;
    setDiscarding(true); setActionError(null);
    try { await sims.discard(sim.id); inv.reload(); } catch (e) { setActionError(sims.refusal(e).message); } finally { setDiscarding(false); }
  };

  // A scenario built by hand runs when the supplier says so; one the agent works on is run by the agent.
  const canRun = !STATIC && sim.status === "created" && phase.kind === "manual";
  const canVary = !STATIC && sim.status !== "discarded" && sim.status !== "running" && phase.kind !== "queued" && phase.kind !== "running";
  const runIt = async () => {
    setRunning(true); setActionError(null);
    try { await sims.run(sim.id); } catch (e) { setActionError(sims.refusal(e).message); } finally { setRunning(false); inv.reload(); }
  };

  return (
    <div className="hypothetical-page">
      <div className="crumb"><Link to={`/lots/${sim.lot_id}`}>← Back to your assessment</Link></div>
      <HypotheticalBanner />
      <div className="page-head">
        <div style={{ minWidth: 0 }}>
          <div className="title-row"><span className="pill sm hypo-tag">What if</span>
            <h1>{question ?? sim.title ?? `Simulation ${sim.id}`}</h1></div>
          <div className="meta">
            <span>Simulated on {fmtDate(sim.created_at)}</span>
            <span>Judged as of {fmtDate(sim.as_of, false)}</span>
            <span>From <Link to={`/lots/${sim.lot_id}`}>your assessment</Link>{sim.baseline.recommendation ? `: ${sim.baseline.recommendation}` : ""}</span>
            {sim.parent_id && <span>A variation of <Link to={`/lots/${sim.lot_id}/simulations/${sim.parent_id}`}>scenario {sim.parent_id}</Link>, on the same frozen copy</span>}
          </div>
        </div>
        <div className="actions">
          {canRun && <button className="btn primary" disabled={running} onClick={runIt}>{running ? "Running…" : "Run this scenario"}</button>}
          {canVary && <Link className="btn" to={`/lots/${sim.lot_id}/scenarios/new?from=${sim.id}`}>Try a variation</Link>}
          {canDiscard && <button className="btn" disabled={discarding} onClick={discard}>Discard</button>}
        </div>
      </div>
      <ErrorNote error={actionError} />
      {inv.error && <div className="error-note" role="alert">{inv.error} <button className="btn sm" onClick={inv.reload}>Try again</button></div>}
      <BaselineWarnings sim={sim} />
      <PhaseBanner phase={phase} lotId={sim.lot_id} />

      {canRun && <div className="banner info" role="status"><span className="b-icon">i</span><div className="body">
        <strong>Not run yet</strong><p>Run it to see the impact. Running uses the rules only, costs nothing and changes nothing live.</p></div></div>}

      {sim.changes.length > 0 && <Card title="The hypothesis" note={`${sim.changes.length} change${sim.changes.length > 1 ? "s" : ""}, applied to the frozen copy only`}>
        <ol className="hypothesis" aria-label="Hypothetical changes">{sim.changes.map((c, i) => <li key={i}>{describeChange(c, statement)}</li>)}</ol>
      </Card>}

      {sim.status === "completed" && sim.summary && results && <>
        <ImpactSummary s={sim.summary} />
        {trace?.outcome?.outcome === "answered" && <AgentAnswer o={trace.outcome} />}
        <Comparison rows={results} simulationId={id} />
        <DriftAndLimits s={sim.summary} rows={results} />
      </>}

      {trace && trace.steps.length > 0 && <TraceCard steps={trace.steps} outcome={trace.outcome} statement={statement} live={phase.kind === "running"} />}
    </div>
  );
}

function BaselineWarnings({ sim }: { sim: Simulation }) {
  const notes: string[] = [];
  if (sim.baseline.superseded) notes.push("Your assessment has been updated since this simulation was made, so it compares against an earlier result.");
  if (sim.drift.baseline_on_current_version === false) notes.push("The buyer has published a newer version of the tender since your assessment ran.");
  const facts = sim.drift.facts_changed_since_baseline ?? [];
  if (facts.length) notes.push(`${facts.length} of your profile facts changed after your assessment ran. The simulation uses your profile as it was when the simulation was made.`);
  if (!notes.length) return null;
  return <div className="banner warn"><span className="b-icon">!</span><div className="body"><strong>Read this result with care</strong>
    {notes.map((n, i) => <p key={i}>{n}</p>)}</div></div>;
}

function PhaseBanner({ phase, lotId }: { phase: Phase; lotId: number }) {
  const again = <Link className="btn" to={`/lots/${lotId}`}>Ask again</Link>;
  switch (phase.kind) {
    case "queued": return <div className="banner info" role="status"><span className="spinner dark" aria-hidden /><div className="body">
      <strong>Queued</strong>
      <p>{phase.waitingBehind ? `Waiting behind ${phase.waitingBehind} task${phase.waitingBehind > 1 ? "s" : ""}. Assessments always go first.` : "Waiting to start."}
        {" "}You can leave this page; the investigation carries on.</p></div></div>;
    case "running": return <div className="banner info" role="status"><span className="spinner dark" aria-hidden /><div className="body">
      <strong>Investigating…</strong><p>This takes up to about 2½ minutes. The steps appear below as they are recorded. You can leave this page.</p></div></div>;
    case "answered": return null;
    case "ended": return <div className={`banner ${phase.tone}`} role="status"><span className="b-icon">{phase.tone === "warn" ? "!" : "i"}</span><div className="body">
      <strong>{phase.title}</strong><p>{phase.detail}</p></div>{phase.tone === "warn" && again}</div>;
    case "failed": return <div className="banner warn" role="alert"><span className="b-icon">!</span><div className="body">
      <strong>{phase.title}</strong><p>{phase.detail}{phase.detail ? " " : ""}Nothing live was changed.</p></div>{again}</div>;
    case "interrupted": return <div className="banner warn" role="alert"><span className="b-icon">!</span><div className="body">
      <strong>Interrupted</strong><p>{phase.detail}</p></div>{again}</div>;
    case "discarded": return <div className="banner info" role="status"><span className="b-icon">i</span><div className="body">
      <strong>Discarded</strong><p>This simulation was discarded. It is kept for the record only.</p></div></div>;
    case "manual": return null; // a simulation built by hand: no investigation to report
  }
}

function ImpactSummary({ s }: { s: Summary }) {
  const rec = s.recommendation;
  const changed = s.changed.length;
  return <Card title="Impact" note="Hypothetical">
    <div className="rec-compare">
      <div><div className="label">Without the changes</div><div className="rec">{rec.control}</div></div>
      <div className="arrow" aria-hidden>→</div>
      <div><div className="label">With the changes (hypothetical)</div><div className="rec hypo-text">{rec.simulated}</div></div>
    </div>
    {rec.drift && <p className="small warn-text">"Without the changes" differs from your assessment ({rec.baseline}) because something live changed since it ran. See "What else changed" below; that difference is not caused by the hypothesis.</p>}
    {!rec.complete && <p className="small warn-text">Not final: some requirements need reading by the AI model, which the rules cannot do. The recommendation may move once they are read.</p>}
    <div className="tiles four">
      <Tile label="Verdicts changed" value={changed} tone={changed ? "info" : ""} detail={changed ? "by the hypothetical changes" : "The changes move no verdict"} />
      <Tile label="Need reading" value={s.needs_model.length} tone={s.needs_model.length ? "warn" : ""} detail="the rules can't decide these" />
      <Tile label="Not reached" value={s.not_affected.length} detail="no change touches them" />
      <Tile label="Differ for other reasons" value={s.drift.length} tone={s.drift.length ? "warn" : ""} detail="live changes since your assessment" />
    </div>
  </Card>;
}

function AgentAnswer({ o }: { o: Outcome }) {
  return <Card title="The answer" note="Checked against the results below">
    {o.changed && <>
      <div className="label" style={{ marginTop: 0 }}>Verified changes</div>
      {o.changed.length === 0 ? <p className="small">No verdict changes.</p> :
        <ul className="plain-list compact">{o.changed.map((c) => <li key={c.requirement_id}><span>{c.statement}</span>
          <span className="verdict-move"><SimVerdict status={c.from} /> → <SimVerdict status={c.to} /></span></li>)}</ul>}
    </>}
    {o.recommendation && <p className="small"><strong>Recommendation:</strong> {o.recommendation.before} → <span className="hypo-text">{o.recommendation.after}</span></p>}
    {o.answer && <>
      <div className="label">Explanation, written by the AI model</div>
      <p className="prose model-text">{o.answer}</p>
    </>}
    {(o.caveats?.length ?? 0) > 0 && <>
      <div className="label">Caveats</div>
      <ul className="caveats">{o.caveats!.map((c, i) => <li key={i}>{c}</li>)}</ul>
    </>}
  </Card>;
}

function Comparison({ rows, simulationId }: { rows: ResultRow[]; simulationId: number }) {
  const changed = rows.filter((r) => r.effect);
  const rest = rows.filter((r) => !r.effect);
  return <Card flush title="Requirement by requirement" note={`${changed.length} changed · ${rest.length} unchanged`}>
    {changed.length === 0 && <Empty>No requirement's verdict changes under this hypothesis.</Empty>}
    {changed.length > 0 && <ComparisonTable simulationId={simulationId} rows={changed} caption="Requirements whose verdict changes" />}
    {rest.length > 0 && <details className="more unchanged" open={changed.length === 0}>
      <summary>Unchanged requirements · {rest.length}</summary>
      <ComparisonTable simulationId={simulationId} rows={rest} caption="Requirements whose verdict does not change" />
    </details>}

  </Card>;
}

function ComparisonTable({ rows, caption, simulationId }: { rows: ResultRow[]; caption: string; simulationId: number }) {
  return <div className="table-wrap"><table className="sim-table">
    <caption className="sr-only">{caption}</caption>
    <thead><tr><th>Requirement</th><th>Your assessment</th><th>Without changes</th><th>With changes</th><th>Why</th></tr></thead>
    <tbody>{rows.map((r) => <tr key={r.requirement_id} className={r.effect ? "effect" : r.drift ? "attention" : ""}>
      <td><div style={{ fontWeight: 500 }}>{r.statement}</div><div className="small muted">{r.obligation === "mandatory" ? "Mandatory" : r.obligation === "scored" ? "Scored" : "Information"}
        {r.effect && <Tag tone="info">Changed</Tag>}{r.drift && <Tag tone="warn">Differs for other reasons</Tag>}</div></td>
      <td><SimVerdict status={r.baseline_status} real /></td>
      <td><SimVerdict status={r.control_status} /></td>
      <td><SimVerdict status={r.simulated_status} /></td>
      <td className="small"><Why r={r} /><Provenance row={r} simulationId={simulationId} /></td>
    </tr>)}</tbody>
  </table></div>;
}

function Why({ r }: { r: ResultRow }) {
  if (r.how === "not_affected") return <span className="muted">No change reaches it, so its verdict stands{r.control_status === null ? " as in your assessment" : ""}.</span>;
  if (r.how === "needs_model") return <>
    <div><strong>Needs reading.</strong> A change reaches it, but the rules can't decide this one; only the AI model reading the text could. Nothing was guessed.</div>
    {r.reached.map((x, i) => <div key={i} className="muted">Reached: {x.detail}</div>)}
  </>;
  return <>
    {r.reason_simulated && <div><strong>With changes:</strong> {r.reason_simulated}</div>}
    {r.reason_control && r.reason_control !== r.reason_simulated && <div className="muted"><strong>Without:</strong> {r.reason_control}</div>}
    {r.reached.map((x, i) => <div key={i} className="muted">Reached: {x.detail}</div>)}
    {r.drift && <div className="warn-text">Differs from your assessment for reasons other than the hypothesis.</div>}
    {r.notes.map((n, i) => <div key={i} className="muted">{n}</div>)}
    {r.input_drift.length > 0 && <div className="warn-text">Live input changed: {[...new Set(r.input_drift.map((d) => INPUT[d.input] ?? d.input))].join(", ")}.</div>}
  </>;
}

function DriftAndLimits({ s, rows }: { s: Summary; rows: ResultRow[] }) {
  const name = (rid: number | null) => rid === null ? "all requirements" : rows.find((r) => r.requirement_id === rid)?.statement ?? `requirement ${rid}`;
  const sinceCreation = s.input_drift.since_creation;
  const sinceBaseline = s.input_drift.since_baseline;
  const any = sinceCreation.length + sinceBaseline.length + s.drift.length + s.stale_baseline.length > 0;
  return <Card title="What else changed, and the limits of this result">
    {!any && <p className="small" style={{ marginTop: 0 }}>Nothing live changed between your assessment and this simulation, so every difference above comes from the hypothesis.</p>}
    {sinceCreation.length > 0 && <div className="drift-block" role="note"><div className="label">Changed since this simulation was made</div>
      <ul className="caveats">{sinceCreation.map((d, i) => <li key={i}>{d.detail}{d.requirement_id !== null ? ` (${name(d.requirement_id)})` : ""}</li>)}</ul></div>}
    {sinceBaseline.length > 0 && <div className="drift-block" role="note"><div className="label">Recorded after your assessment ran</div>
      <ul className="caveats">{sinceBaseline.map((d, i) => <li key={i}>{d.detail} ({name(d.requirement_id)})</li>)}</ul></div>}
    {s.drift.length > 0 && <div className="drift-block" role="note"><div className="label">Verdicts that differ from your assessment for other reasons</div>
      <ul className="caveats">{s.drift.map((d) => <li key={d.requirement_id}>{name(d.requirement_id)}</li>)}</ul></div>}
    {s.stale_baseline.length > 0 && <p className="small warn-text">Your assessment's verdict may be out of date for: {s.stale_baseline.map(name).join("; ")}.</p>}
    {s.execution && <details className="more"><summary>What the simulation froze, and what it read live</summary>
      <div className="small">Frozen when the simulation was made: {s.execution.frozen.join(", ")}.</div>
      <div className="small">Read live when it ran: {Object.keys(s.execution.live_inputs).map((k) => INPUT[k] ?? k).join(", ")}.</div>
    </details>}
  </Card>;
}

function TraceCard({ steps, outcome, statement, live }: { steps: Step[]; outcome: Outcome | null; statement: (id: number) => string | undefined; live: boolean }) {
  const total = steps.reduce((t, s) => t + s.cost_usd, 0);
  const estimated = steps.some((s) => s.cost_estimated);
  return <Card title="How this was worked out" note={`${steps.length} recorded step${steps.length === 1 ? "" : "s"} · AI cost ${usd(total)}${estimated ? " (partly estimated)" : ""}`}>
    <ol className="trace" aria-live={live ? "polite" : undefined}>
      {steps.map((s) => {
        const d = describeStep(s, statement);
        return <li key={s.step} className={d.problem ? "problem" : ""}>
          <div className="trace-head"><span className="trace-n">{s.step}</span><strong>{d.title}</strong>
            {s.cost_usd > 0 && <span className="small muted">{usd(s.cost_usd)}{s.cost_estimated ? " (estimated)" : ""}</span>}</div>
          {d.detail && <div className="small">{d.detail}</div>}
          {d.problem && <div className="small warn-text">{d.problem}</div>}
          <details className="raw"><summary>Details</summary><pre>{JSON.stringify({ input: s.input, output: s.output }, null, 2)}</pre></details>
        </li>;
      })}
    </ol>
    {outcome && outcome.outcome !== "answered" && outcome.reason && <p className="small muted">Ended: {outcome.reason}</p>}
  </Card>;
}
