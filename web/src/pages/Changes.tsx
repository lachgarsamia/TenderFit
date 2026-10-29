import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useProfile } from "../App";
import { api, fmtDate } from "../api";
import { Card, Empty, ErrorNote, Tag, VerdictBadge } from "../components/ui";

const PATH_LABEL: Record<string, string> = {
  "notice.deadline": "Submission deadline", "notice.title": "Title", "notice.value": "Estimated value", "notice.cpv": "CPV codes",
  "lot.regions": "Delivery regions", "lot.quantity": "Lot quantity", "lot.title": "Lot title", "lot.description": "Lot description",
};

function show(v: any): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "string" && /^\d{4}-\d{2}-\d{2}T/.test(v)) return fmtDate(v);
  if (Array.isArray(v)) return v.join(", ");
  if (typeof v === "object") return v.amount ? `£${Number(v.amount).toLocaleString("en-GB")}` : JSON.stringify(v);
  return String(v);
}

export default function Changes() {
  const { profile } = useProfile();
  const [events, setEvents] = useState<any[] | null>(null);
  const [sel, setSel] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api<any[]>(`/changes?profile_id=${profile!.id}`).then((e) => { setEvents(e); setSel(e.find((x) => x.kind !== "status")?.id ?? e[0]?.id ?? null); })
      .catch((e) => setError(e.message));
  }, [profile?.id]);

  const ev = events?.find((e) => e.id === sel);
  const others = (events ?? []).filter((e) => e.id !== sel);
  const imp = ev?.impacts?.[0];
  const [head, ...lines] = (imp?.explanation ?? "").split("\n");

  return (
    <>
      <div className="crumb">Changes{ev ? ` / ${ev.buyer} · ${ev.opportunity}` : ""}</div>
      <h1>What changed, and what it means for you</h1>
      <p className="sub">Find a Tender keeps earlier versions of an edited tender notice, so both versions stay citable. Only published changes are tracked automatically; upload anything the buyer sent you directly.</p>
      <ErrorNote error={error} />
      {!events ? <Empty>Loading…</Empty> : events.length === 0 ? <Card><Empty>No changes yet for assessed opportunities. New notice versions are detected on each import.</Empty></Card> : (
        <div className="grid-main">
          <div>
            {ev && ev.kind === "version" && <>
              <div className="versions">
                <div className="version"><div className="small muted">VERSION 1 · {ev.from_release}</div><strong>Published {fmtDate(ev.from_published, false)}</strong></div>
                <span aria-hidden>⟶</span>
                <div className="version current"><div className="small" style={{ color: "#b0651f" }}>VERSION 2 · CURRENT · {ev.to_release}</div><strong>Edited {fmtDate(ev.to_published, false)}</strong></div>
                <Tag tone={ev.cls === "local" ? "info" : "warn"}>{ev.cls} change</Tag>
              </div>
              <Card title="Differences between versions" extra={`${ev.fields.length + ev.passages.length} changed${imp ? ` · ${imp.kept} verdicts unchanged` : ""}`}>
                <table>
                  <thead><tr><th>Field · source</th><th>Version 1</th><th>Version 2</th></tr></thead>
                  <tbody>
                    {ev.fields.map((f: any, i: number) => (
                      <tr key={`f${i}`}><td><div style={{ fontWeight: 500 }}>{PATH_LABEL[f.path.split("@")[0]] ?? f.path}{f.path.includes("@") ? ` (lot ${f.path.split("@")[1]})` : ""}</div><div className="mono">Notice · fields</div></td>
                        <td className="strike">{show(f.before)}</td><td className="warn-text">{show(f.after)}</td></tr>
                    ))}
                    {ev.passages.map((p: any, i: number) => (
                      <tr key={`p${i}`}><td><div style={{ fontWeight: 500 }}>{p.section ?? "Passage"}</div><div className="mono">{p.document}</div></td>
                        <td className="small strike">{p.kind === "added" ? "—" : p.before || "earlier wording"}</td>
                        <td className="small"><span className="warn-text">{p.kind === "removed" ? "removed" : p.after}</span>
                          <div className="muted">passage {p.kind}{p.kind === "modified" ? ` · ${Math.round(p.confidence)}% aligned` : ""}</div></td></tr>
                    ))}
                    {ev.documents.map((d: any, i: number) => (
                      <tr key={`d${i}`}><td><div style={{ fontWeight: 500 }}>{d.label}</div><div className="mono">Notice · documents</div></td>
                        <td className="small muted">{d.kind === "added" ? "—" : "present"}</td><td className="small warn-text">document {d.kind}</td></tr>
                    ))}
                  </tbody>
                </table>
                {ev.reasons.length > 0 && <div className="small muted" style={{ marginTop: 8 }}>Classified {ev.cls}: {ev.reasons.join("; ")}</div>}
              </Card>
            </>}
            {ev && ev.kind === "profile" && <Card title="Change in your profile"><div>{ev.reasons.join("; ")}</div></Card>}
            {imp && (
              <Card title={`Effect on your assessment of Lot ${imp.lot_no}${imp.lot_title ? ` · ${imp.lot_title}` : ""}`}>
                {imp.affected.map((x: any) => (
                  <div key={x.requirement_id} style={{ marginBottom: 12 }}>
                    <div style={{ fontWeight: 500, marginBottom: 6 }}>{x.statement} <span className="small muted">· {x.how}</span></div>
                    <div className="before-after">
                      <div className="ba"><div className="k">Before{ev.kind === "version" ? " · version 1" : ""}</div>{x.before ? <VerdictBadge status={x.before} small /> : <Tag>new requirement</Tag>}</div>
                      <div className="ba after"><div className="k">After{ev.kind === "version" ? " · version 2" : ""}</div><VerdictBadge status={x.after} small /><div className="small" style={{ marginTop: 6 }}>{x.reason}</div></div>
                    </div>
                  </div>
                ))}
                <div className="before-after">
                  <div className="ba"><div className="k">Recommendation before</div><div className="small">{imp.before_recommendation}</div></div>
                  <div className="ba after"><div className="k">Recommendation after</div><div className="small">{imp.after_recommendation}</div></div>
                </div>
                {imp.requirements_added.length + imp.requirements_removed.length > 0 && <div className="small" style={{ marginTop: 10 }}>
                  {imp.requirements_added.length > 0 && <>New: {imp.requirements_added.map((x: any) => x.statement).join("; ")}. </>}
                  {imp.requirements_removed.length > 0 && <>Removed: {imp.requirements_removed.map((x: any) => x.statement).join("; ")}.</>}
                </div>}
              </Card>
            )}
          </div>
          <div>
            {imp && <Card title="Why the recommendation changed">
              <p style={{ marginTop: 0 }}>{head}</p>
              <ul className="steps">{lines.map((l: string, i: number) => <li key={i}>{l.replace(/^- /, "")}</li>)}</ul>
              {imp.audit && <div className="small" style={{ marginTop: 8 }}>{imp.audit.agrees ? "✓ Background full reassessment agrees with the targeted one." :
                `! Full reassessment disagreed on ${imp.audit.missed_dependencies.length} mandatory requirement(s); logged as a missed dependency.`}</div>}
              <Link className="btn primary" style={{ marginTop: 10 }} to={`/lots/${imp.lot_id}`}>Open updated assessment</Link>
            </Card>}
            <Card title="Other changes">
              {others.length === 0 ? <div className="small muted">None.</div> : others.map((o) => (
                <div key={o.id} className={`list-change ${o.id === sel ? "selected" : ""}`} onClick={() => setSel(o.id)}>
                  <div style={{ fontWeight: 500 }}>{o.buyer} · {o.opportunity}</div>
                  {o.kind === "status" ? <div className="bad-text small">{o.summary.status === "cancelled" ? "Cancelled" : "Awarded"} · {fmtDate(o.detected_at, false)}</div>
                    : <div className="small muted">{o.kind === "profile" ? "Profile change" : "New notice version"} · {fmtDate(o.detected_at, false)}</div>}
                  <div className="small muted">{o.reasons.join("; ")}</div>
                </div>
              ))}
            </Card>
          </div>
        </div>
      )}
    </>
  );
}
