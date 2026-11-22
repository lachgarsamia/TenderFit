import { useEffect, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { usePageTitle } from "../App";
import { STATIC, fmtDate } from "../api";
import { SourceDialog, type SourceRef } from "../components/SourceDialog";
import { Card, Empty, ErrorNote, Skeleton, Tag } from "../components/ui";
import { Counterfactuals } from "./Counterfactuals";
import * as sims from "./api";
import type { Cell, ComparedScenario, Comparison, Direction, PriorityEntry } from "./api";
import { HypotheticalBanner, SimVerdict } from "./SimulationPage";
import { INPUT, describeChange } from "./describe";

const DIRECTION: Record<Direction, [string, string, string]> = {
  improved: ["↑", "Better", "ok"], worsened: ["↓", "Worse", "bad"], unchanged: ["=", "No change", "muted"],
  undetermined: ["?", "Needs reading", "warn"],
};
const HIGHLIGHT: [keyof ComparedScenario["highlights"], string, string][] = [
  ["blockers_resolved", "Mandatory blockers cleared", "ok"],
  ["blockers_now_unknown", "Blockers that become unknown (not cleared)", "warn"],
  ["blockers_introduced", "New mandatory blockers", "bad"],
  ["uncertainties_resolved", "Unknowns now decided", "info"],
  ["now_needs_confirmation", "Would still need you to confirm or prove", "warn"],
  ["needs_model", "Reached, but only AI reading could decide", "warn"],
];
const PRIORITY: [keyof Omit<Comparison["priorities"], "basis">, string, string][] = [
  ["addresses_blockers", "Changes that clear a mandatory blocker", "The verdict moved from not met to met or needs confirmation."],
  ["resolves_uncertainty", "Changes that settle an unknown", "A requirement nothing proved either way is now decided."],
  ["widest_reach", "Changes that reach the most requirements", "Reached in the dependency analysis, whether or not the verdict moved."],
  ["depends_on_evidence_not_held", "Changes that depend on evidence you don't hold", "Their effect only follows if you obtain that evidence."],
  ["introduces_blockers", "Changes that create a blocker", "A verdict moved to not met on a mandatory requirement."],
  ["little_or_no_effect", "Changes with little or no effect", "No verdict they reach moves."],
];

export function Direction({ d }: { d: Direction }) {
  const [icon, label, tone] = DIRECTION[d];
  return <span className={`pill sm ${tone} outline-dir`}><span aria-hidden>{icon}</span> {label}</span>;
}

/** Two to four completed scenarios of one assessment, side by side. Every verdict category stays distinct, transitions
 * are counted by name, and nothing is combined into a score. */
export default function ComparePage() {
  const { lotId = "" } = useParams();
  const [params] = useSearchParams();
  const ids = (params.get("ids") ?? "").split(",").map(Number).filter((n) => Number.isInteger(n) && n > 0);
  const key = ids.join(",");
  const [data, setData] = useState<Comparison | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [source, setSource] = useState<SourceRef | null>(null);
  usePageTitle("Compare scenarios");

  useEffect(() => {
    let cancelled = false;
    setData(null); setError(null);
    if (STATIC) { setError("Scenarios are switched off in the read-only demo."); return; }
    if (ids.length < 2) { setError("Choose at least two scenarios to compare."); return; }
    sims.compare(lotId, ids).then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(sims.refusal(e).message); });
    return () => { cancelled = true; };
  }, [lotId, key]);

  const back = <div className="crumb"><Link to={`/lots/${lotId}`}>← Back to your assessment</Link></div>;
  if (error) return <div className="hypothetical-page">{back}<HypotheticalBanner /><ErrorNote error={error} /></div>;
  if (!data) return <div className="hypothetical-page">{back}<HypotheticalBanner /><Card><Skeleton lines={6} /></Card></div>;

  const ordered = data.order.map((id) => data.scenarios.find((s) => s.id === id)!).filter(Boolean);
  const names = new Map(data.scenarios.map((s) => [s.id, s.title ?? `Scenario ${s.id}`]));
  const statement = (rid: number) => data.requirements.find((r) => r.requirement_id === rid)?.statement;
  const fact = (id: number) => data.facts[String(id)];
  const affected = data.requirements.filter((r) => r.affected);
  const untouched = data.requirements.filter((r) => !r.affected);

  return <div className="hypothetical-page">
    {back}
    <HypotheticalBanner />
    <div className="page-head"><div style={{ minWidth: 0 }}>
      <div className="title-row"><span className="pill sm hypo-tag">What if</span><h1>Compare {data.scenarios.length} scenarios</h1></div>
      <div className="meta"><span>All start from <Link to={`/lots/${lotId}`}>your assessment</Link></span>
        <span>Each is compared with the same frozen copy without its changes</span></div>
    </div></div>

    <Integrity c={data} />

    <Card title="Scenarios" note="Listed in the order explained below">
      <div className="scenario-cols">{ordered.map((s) => <ScenarioCard key={s.id} s={s} statement={statement} fact={fact} parent={s.parent_id ? names.get(s.parent_id) ?? `scenario ${s.parent_id}` : null} lotId={lotId} />)}</div>
      <p className="small muted">{data.ordering_rule}</p>
    </Card>

    <Card flush title="Requirement by requirement" note={`${affected.length} affected by at least one scenario · ${untouched.length} affected by none`}>
      {affected.length === 0 && <Empty>No scenario moves any requirement's verdict.</Empty>}
      {affected.length > 0 && <CompareTable c={data} rows={affected} order={ordered} onSource={setSource} caption="Requirements at least one scenario affects" />}
      {untouched.length > 0 && <details className="more unchanged" open={affected.length === 0}>
        <summary>Not affected by any scenario · {untouched.length}</summary>
        <CompareTable c={data} rows={untouched} order={ordered} onSource={setSource} caption="Requirements no scenario affects" />
      </details>}
      <p className="small muted" style={{ padding: "0 20px 14px" }}>Better and worse follow this order: not met, unknown, needs confirmation, met. A change from "not met" to "unknown" is listed as better only because nothing contradicts the requirement any more; it is not proof.</p>
    </Card>

    <Priorities c={data} names={names} statement={statement} fact={fact} />
    {source && <SourceDialog source={source} onClose={() => setSource(null)} />}
  </div>;
}

function Integrity({ c }: { c: Comparison }) {
  if (c.integrity.consistent) return <div className="banner ok" role="status"><span className="b-icon">✓</span><div className="body">
    <strong>Compared on identical inputs</strong><p>These scenarios share one frozen copy of your assessment, and nothing live changed between their runs.</p></div></div>;
  return <div className="banner warn" role="alert"><span className="b-icon">!</span><div className="body">
    <strong>Read this comparison with care</strong>{c.integrity.notes.map((n, i) => <p key={i}>{n}</p>)}</div></div>;
}

type Name = (id: number) => string | undefined;

function ScenarioCard({ s, statement, fact, parent, lotId }: { s: ComparedScenario; statement: Name; fact: Name; parent: string | null; lotId: string }) {
  const transitions = Object.entries(s.transitions);
  const drift = s.input_drift.since_creation.length + s.input_drift.since_baseline.length;
  return <section className="scenario-col" aria-label={s.title ?? `Scenario ${s.id}`}>
    <h3><Link to={`/lots/${lotId}/simulations/${s.id}`}>{s.title ?? `Scenario ${s.id}`}</Link></h3>
    {parent && <div className="small muted">Variation of {parent}</div>}
    <div className="small muted">Judged as of {fmtDate(s.as_of, false)}</div>
    <ol className="hypothesis compact" aria-label="Hypothetical changes">
      {s.changes.length === 0 ? <li className="muted">No changes</li> : s.changes.map((c) => <li key={c.ordinal}>{describeChange(c.change, statement, fact)}
        {c.assumes_evidence && <Tag tone="warn">Supposes evidence</Tag>}</li>)}
    </ol>
    {s.recommendation && <div className="small"><div className="label">Recommendation</div>
      <div>{s.recommendation.control}</div><div aria-hidden>↓</div><div className="hypo-text">{s.recommendation.simulated}</div>
      {!s.recommendation.complete && <div className="warn-text">Not final: some requirements need AI reading.</div>}</div>}
    <dl className="counts">
      {(["improved", "worsened", "unchanged", "undetermined"] as Direction[]).map((d) => <div key={d}><dt><Direction d={d} /></dt><dd>{s.counts[d]}</dd></div>)}
    </dl>
    {transitions.length > 0 && <div className="small"><div className="label">Verdict moves</div>
      <ul className="plain-list compact">{transitions.map(([t, n]) => {
        const [from, to] = t.split(" -> ") as [never, never];
        return <li key={t}><span className="verdict-move"><SimVerdict status={from} /> → <SimVerdict status={to} /></span><span>× {n}</span></li>;
      })}</ul></div>}
    {(s.override_changes ?? []).map((c) => <p key={c.requirement_id} className="small warn-text">Hypothetical override {c.after ? "on" : "off"} for {statement(c.requirement_id)}. Recommendation only; verdict unchanged.</p>)}
    {s.no_effect && <p className="small muted">These changes move no verdict.</p>}
    {HIGHLIGHT.filter(([k]) => s.highlights[k].length).map(([k, label, tone]) => <div key={k} className="small">
      <Tag tone={tone}>{label}</Tag> {s.highlights[k].map((rid) => statement(rid) ?? `requirement ${rid}`).join("; ")}</div>)}
    {drift > 0 && <p className="small warn-text">Live inputs changed for this scenario: {[...new Set([...s.input_drift.since_creation, ...s.input_drift.since_baseline].map((d) => INPUT[d.input] ?? d.input))].join(", ")}.</p>}
  </section>;
}

function CompareTable({ c, rows, order, onSource, caption }: { c: Comparison; rows: Comparison["requirements"]; order: ComparedScenario[]; onSource: (s: SourceRef) => void; caption: string }) {
  return <div className="table-wrap"><table className="sim-table compare-table">
    <caption className="sr-only">{caption}</caption>
    <thead><tr><th>Requirement</th><th>Your assessment</th>{order.map((s) => <th key={s.id}>{s.title ?? `Scenario ${s.id}`}</th>)}</tr></thead>
    <tbody>{rows.map((r) => <tr key={r.requirement_id} className={r.differs_between_scenarios ? "effect" : ""}>
      <td><div style={{ fontWeight: 500 }}>{r.statement}</div>
        <div className="small muted">{r.obligation === "mandatory" ? "Mandatory" : r.obligation === "scored" ? "Scored" : "Information"}
          {r.differs_between_scenarios && <Tag tone="info">Scenarios differ</Tag>}</div>
        {r.passage_id && <button type="button" className="link-btn example small" onClick={() => onSource({ passage_id: r.passage_id!, quote: r.quote ?? undefined })}>Tender source</button>}</td>
      <td><SimVerdict status={r.baseline_status} real /></td>
      {order.map((s) => <td key={s.id}><CellView cell={r.scenarios[String(s.id)]} /></td>)}
    </tr>)}</tbody>
  </table>
  <span className="sr-only">{c.verdict_order.join(" < ")}</span></div>;
}

function CellView({ cell }: { cell: Cell | null }) {
  if (!cell) return <span className="muted small">not in this scenario</span>;
  return <div className="cell small">
    <div className="verdict-move"><SimVerdict status={cell.control} /> → <SimVerdict status={cell.simulated} /></div>
    <Direction d={cell.direction} />
    {cell.how === "needs_model" && <div className="muted">A change reaches it, but the rules can't decide it. Nothing was guessed.</div>}
    {cell.how === "not_affected" && <div className="muted">No change of this scenario reaches it.</div>}
    {cell.how === "rule" && cell.reason_simulated && <details className="why"><summary>Why</summary>
      <div><strong>With changes:</strong> {cell.reason_simulated}</div>
      {cell.reason_control && cell.reason_control !== cell.reason_simulated && <div className="muted"><strong>Without:</strong> {cell.reason_control}</div>}
      {cell.reached.map((x, i) => <div key={i} className="muted">Reached: {x.detail}</div>)}
      {cell.notes.map((n, i) => <div key={i} className="muted">{n}</div>)}
    </details>}
    {cell.effect && <Counterfactuals analysis={cell.attribution} />}
    {cell.drift && <div className="warn-text">Differs from your assessment for other reasons too.</div>}
  </div>;
}

function Priorities({ c, names, statement, fact }: { c: Comparison; names: Map<number, string>; statement: Name; fact: Name }) {
  const groups = PRIORITY.filter(([k]) => c.priorities[k].length);
  if (!groups.length) return null;
  const req = (rid: number) => statement(rid) ?? `requirement ${rid}`;
  return <Card title="What the changes did" note="From the dependency analysis; not a forecast">
    {groups.map(([k, title, detail]) => <div key={k} className="priority-group">
      <div className="label">{title}</div><p className="small muted">{detail}</p>
      <ul className="plain-list compact">{c.priorities[k].map((e: PriorityEntry) => <li key={`${e.scenario_id}:${e.ordinal}`}>
        <div style={{ minWidth: 0 }}><div>{describeChange(e.change, statement, fact)}</div>
          <div className="small muted">In {names.get(e.scenario_id)} · reaches {e.reaches.length} · moves {e.moved.length}
            {e.moved.length > e.attributable.length && " (shared with other changes in that scenario)"}
            {e.moved.length > 0 && `: ${e.moved.map(req).join("; ")}`}</div></div>
      </li>)}</ul>
    </div>)}
    <p className="small muted">{c.priorities.basis}</p>
  </Card>;
}
