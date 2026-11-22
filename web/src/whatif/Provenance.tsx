import { useState } from "react";
import { EvidenceBadge, verifiedSupport } from "../components/EvidenceBadge";
import { ErrorNote, Tag } from "../components/ui";
import { describeChange } from "./describe";
import { provenance, type Provenance as Record, type ResolvedProvenance, type ResultRow } from "./api";

/** Inline disclosure avoids modal stacks and keeps evidence next to the rule's explanation. */
export function Provenance({ row, simulationId }: { row: ResultRow; simulationId: number }) {
  const [data, setData] = useState<ResolvedProvenance | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  async function load() {
    if (data || loading) return;
    setLoading(true); setError(null);
    try { setData(await provenance(simulationId, row.requirement_id)); }
    catch { setError("The source details could not be loaded. Your frozen evidence is shown below."); }
    finally { setLoading(false); }
  }
  if (!row.provenance && !row.control_provenance) return <p className="small muted">
    {row.simulated_status === null ? "Model interpretation needed; no rule conclusion or evidence was recorded."
      : "Evidence provenance was not saved for this older result. See your assessment for its sources."}</p>;
  return <details className="more" onToggle={(e) => { if (e.currentTarget.open) void load(); }}>
    <summary>Evidence and assumptions</summary>
    <ErrorNote error={error} />
    {error && <button className="btn sm" onClick={() => void load()}>Retry source details</button>}
    {loading && <p role="status">Loading source details…</p>}
    {row.provenance && <Evidence record={row.provenance} data={data} />}
    {row.control_provenance && <details className="more"><summary>Evidence without changes</summary>
      <Evidence record={row.control_provenance} data={data} /></details>}
  </details>;
}

export function Evidence({ record, data }: { record: Record; data: ResolvedProvenance | null }) {
  return <div className="small provenance">
    <Tag>Rule conclusion</Tag>{record.needs_human && <Tag tone="warn">Needs a person</Tag>}
    {record.citations.map((c, i) => <div key={i}>
      <div className="label">Tender source</div><blockquote>{c.quote}</blockquote>
      {data && (data.passages[c.passage_id] ? <details><summary>Read passage in context</summary>
        <p>{data.passages[c.passage_id]!.filename} · {data.passages[c.passage_id]!.section}</p>
        <p className="prose">{data.passages[c.passage_id]!.text}</p></details> : <p>Source is no longer available to your supplier.</p>)}
    </div>)}
    {record.requirement_assumptions.map((a) => <div key={a.ordinal} className="warn-text">
      <strong>Assumption (this scenario), change {a.ordinal}:</strong> {describeChange(a.change)}
      <p>The tender quote above is the original source. This change is hypothetical.</p>
    </div>)}
    {record.decision_inputs && <div>
      <div className="label">{record.decision_inputs.origin === "modified" ? "Hypothetical answers and override" : "Answers and override frozen at creation"}</div>
      {record.decision_inputs.answers.length ? record.decision_inputs.answers.map((answer, i) => <p key={i}>Declaration, not proof: {answer}</p>) : <p>No answer recorded in this scenario.</p>}
      <p>Override {record.decision_inputs.overridden ? "on" : "off"}: affects the recommendation only, not this verdict.</p>
    </div>}
    {!record.support_available && <p>Evidence references were not frozen in this older snapshot.</p>}
    {record.facts.length === 0 && <p>No supplier fact supports this rule conclusion.</p>}
    {record.facts.map((f) => <div className="fact-block" key={f.id}>
      <strong>{f.label}</strong> <Tag>{f.origin === "actual" ? "Actual" : f.origin === "removed" ? "Removed in this scenario" : f.origin === "modified" ? "Modified" : "Hypothetical"}</Tag>
      <EvidenceBadge status={f.evidence_status} origin={f.origin} verified={f.support.some(verifiedSupport)} />
      <p>{display(f.value)}{f.valid_until ? ` · Valid until ${f.valid_until}` : ""}</p>
      {f.declaration_confirmed && <p>Declaration confirmed; this is not documentary proof.</p>}
      {f.changes.map((c) => <p key={c.ordinal}>Change {c.ordinal}: {difference(c.before, c.after)}</p>)}
      {f.origin === "modified" && f.support.length > 0 && <p className="warn-text">These documents support the original fact, not the assumed change.</p>}
      {f.support.map((s, i) => <div key={i}>
        <p>{data?.evidence[s.evidence_file_id]?.filename ?? `Evidence file ${s.evidence_file_id}`}{s.page !== null ? ` · page ${s.page}` : ""}</p>
        <blockquote>{s.quote}</blockquote>
        {data && !data.evidence[s.evidence_file_id] && <p>File is no longer available to your supplier.</p>}
      </div>)}
    </div>)}
    {record.params.map((p) => <p key={p.field}>{p.field.replace(/^notice\.|^lot\./g, "").replace(/_/g, " ")}: {String(p.value ?? "Unknown")} · {p.origin === "modified" ? "Assumption (this scenario)" : "Actual"}
      {p.changes.map((c) => <span key={c.ordinal}> · Change {c.ordinal}: {display(c.before)} → {display(c.after)}</span>)}</p>)}
    {record.gaps.map((gap, i) => <p className="warn-text" key={i}>{display(gap)}</p>)}
  </div>;
}

function display(value: unknown): string {
  if (value === null || value === undefined) return "None";
  if (Array.isArray(value)) return value.map(display).join(", ");
  if (typeof value === "object") return Object.entries(value).map(([key, v]) => `${key.replace(/_/g, " ")}: ${display(v)}`).join(" · ");
  return String(value).replace(/_/g, " ");
}
function difference(before: unknown, after: unknown): string {
  if (!before) return "Hypothetical fact added";
  if (!after) return "Fact removed from this scenario";
  const old = before as { [key: string]: unknown };
  return Object.entries(after).filter(([key, value]) => JSON.stringify(value) !== JSON.stringify(old[key]))
    .map(([key, value]) => `${key.replace(/_/g, " ")}: ${display(old[key])} → ${display(value)}`).join(" · ");
}
