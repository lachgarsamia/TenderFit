import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { usePageTitle, useProfile } from "../App";
import { api, fmtDate } from "../api";
import { Card, Empty, ErrorNote, Skeleton, Tag, VerdictPill, human } from "../components/ui";

const PATH_LABEL: Record<string, string> = {
  "notice.deadline": "Submission deadline", "notice.title": "Title", "notice.value": "Estimated value", "notice.cpv": "Product codes",
  "lot.regions": "Delivery regions", "lot.quantity": "Quantity", "lot.title": "Lot title", "lot.description": "Lot description",
};
const CLS: Record<string, [string, string]> = { local: ["Targeted re-check", "info"], structural: ["Full re-check", "warn"], ambiguous: ["Full re-check", "warn"],
  status: ["Status change", "bad"], external: ["Upload", "info"] };

function show(v: any): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "string" && /^\d{4}-\d{2}-\d{2}T/.test(v)) return fmtDate(v);
  if (Array.isArray(v)) return v.join(", ");
  if (typeof v === "object") return v.amount ? `£${Number(v.amount).toLocaleString("en-GB")}` : JSON.stringify(v);
  return String(v);
}

export default function Changes() {
  usePageTitle("Changes");
  const { profile } = useProfile();
  const [events, setEvents] = useState<any[] | null>(null);
  const [sel, setSel] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    setEvents(null);
    api<any[]>(`/changes?profile_id=${profile!.id}`).then((e) => { setEvents(e); setSel(e.find((x) => x.kind !== "status")?.id ?? e[0]?.id ?? null); })
      .catch((e) => setError(e.message));
  }, [profile?.id]);

  const ev = events?.find((e) => e.id === sel);
  const imp = ev?.impacts?.[0];
  const [head, ...lines] = (imp?.explanation ?? "").split("\n");
  const rec = lines.map((l: string) => l.replace(/^- /, "")).find((l: string) => l.startsWith("Recommendation"));

  return (
    <>
      <div className="page-head">
        <div>
          <h1>What changed, and what it means for you</h1>
          <p className="lede">When a buyer edits a tender, TenderFit compares the versions and re-checks only what the change touches. Changes sent to you directly are not visible here: upload them on the lot.</p>
        </div>
      </div>
      <ErrorNote error={error} />
      {!events ? <Card><Skeleton /></Card> : events.length === 0 ? <Card><Empty title="No changes yet">New versions of tenders you assessed appear here after each import.</Empty></Card> : (
        <div className="split">
          <div style={{ minWidth: 0 }}>
            {ev?.kind === "version" && <div className="versions">
              <div className="version"><div className="k">Version 1</div><strong>Published {fmtDate(ev.from_published, false)}</strong></div>
              <span aria-hidden>→</span>
              <div className="version current"><div className="k">Version 2 · current</div><strong>Edited {fmtDate(ev.to_published, false)}</strong></div>
            </div>}
            {ev?.kind === "version" && <Card flush title="Differences between versions" note={imp ? `${imp.kept} other verdicts unchanged` : undefined}>
              <table>
                <thead><tr><th>What</th><th>Before</th><th>After</th></tr></thead>
                <tbody>
                  {ev.fields.map((f: any, i: number) => (
                    <tr key={`f${i}`}><td><div style={{ fontWeight: 500 }}>{PATH_LABEL[f.path.split("@")[0]] ?? f.path}</div><div className="mono">Notice</div></td>
                      <td className="strike">{show(f.before)}</td><td className="status-warn">{show(f.after)}</td></tr>
                  ))}
                  {ev.passages.filter((p: any) => !p.section?.startsWith("Notice: submission")).map((p: any, i: number) => (
                    <tr key={`p${i}`}><td><div style={{ fontWeight: 500 }}>{p.section ?? "Passage"}</div><div className="mono">{p.document}</div></td>
                      <td className="small strike">{p.kind === "added" ? "—" : p.before}</td>
                      <td className="small status-warn">{p.kind === "removed" ? "Removed" : p.after}</td></tr>
                  ))}
                  {ev.documents.map((d: any, i: number) => (
                    <tr key={`d${i}`}><td><div style={{ fontWeight: 500 }}>{d.label}</div><div className="mono">Documents</div></td>
                      <td className="small muted">{d.kind === "added" ? "—" : "Present"}</td><td className="small status-warn">Document {d.kind}</td></tr>
                  ))}
                </tbody>
              </table>
            </Card>}
            {ev?.kind === "profile" && <Card title="A fact in your profile changed"><div>{ev.reasons.join("; ")}</div></Card>}
            {ev?.kind === "status" && <Card title={`${ev.buyer} · ${ev.opportunity}`}><div className="status-bad">{ev.reasons.join("; ")}</div>
              <p className="small muted">Removed from your shortlist; the earlier assessment is archived.</p></Card>}
            {imp && <Card title={`Effect on Lot ${imp.lot_no}${imp.lot_title ? ` · ${imp.lot_title}` : ""}`}>
              {imp.affected.map((x: any) => (
                <div key={x.requirement_id} style={{ marginBottom: 14 }}>
                  <div style={{ fontWeight: 500 }}>{x.statement}</div>
                  <div className="ba">
                    <div><div className="k">Before</div>{x.before ? <VerdictPill status={x.before} /> : <Tag>New requirement</Tag>}</div>
                    <div className="after"><div className="k">After</div><VerdictPill status={x.after} /><div className="small" style={{ marginTop: 6 }}>{human(x.reason)}</div></div>
                  </div>
                </div>
              ))}
              <div className="ba">
                <div><div className="k">Recommendation before</div>{imp.before_recommendation}</div>
                <div className="after"><div className="k">Recommendation now</div>{imp.after_recommendation}</div>
              </div>
            </Card>}
          </div>
          <div className="sticky">
            {imp && <Card title="Why the recommendation changed">
              <p style={{ marginTop: 0 }}>{head.replace(/\((\d) by rule, no model call\)/, "($1 by a fixed rule)")}</p>
              {rec && <p className="small">The recommendation moved from <strong>{imp.before_recommendation}</strong> to <strong>{imp.after_recommendation}</strong>.</p>}
              {imp.audit && <p className="small">{imp.audit.agrees ? "✓ A full re-check in the background agreed." : "! A full re-check disagreed; TenderFit logged it for review."}</p>}
              <Link className="btn primary" to={`/lots/${imp.lot_id}`}>Open updated assessment</Link>
            </Card>}
            <Card title="All changes">
              {events.map((o) => (
                <button key={o.id} className="change-item" aria-current={o.id === sel} onClick={() => setSel(o.id)}>
                  <div className="small muted">{fmtDate(o.detected_at, false)} · <Tag tone={CLS[o.cls]?.[1]}>{o.kind === "profile" ? "Profile" : CLS[o.cls]?.[0]}</Tag></div>
                  <div style={{ fontWeight: 500 }}>{o.buyer}</div>
                  <div className="small muted">{o.opportunity}</div>
                </button>
              ))}
            </Card>
          </div>
        </div>
      )}
    </>
  );
}
