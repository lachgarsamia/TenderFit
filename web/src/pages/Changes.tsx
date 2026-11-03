import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { usePageTitle, useProfile } from "../App";
import { api, fmtDate, post } from "../api";
import { Card, Empty, ErrorNote, Skeleton, Tag, VerdictPill, human } from "../components/ui";

const PATH_LABEL: Record<string, string> = {
  "notice.deadline": "Submission deadline", "notice.title": "Title", "notice.value": "Estimated value", "notice.cpv": "Product codes",
  "lot.regions": "Delivery regions", "lot.quantity": "Quantity", "lot.title": "Lot title", "lot.description": "Lot description",
};
const CLS: Record<string, [string, string]> = { local: ["Targeted re-check", "info"], structural: ["Full re-check", "warn"], ambiguous: ["Full re-check", "warn"], status: ["Status change", "bad"] };
const STATE: Record<string, [string, string]> = { detected: ["Waiting to be processed", "info"], processing: ["Being processed", "info"], failed: ["Could not finish", "bad"], superseded: ["Replaced by a newer notice", "muted"], applied: ["Applied", "ok"] };
const HOW: Record<string, string> = { "rule only": "A fixed rule re-checked this", "re-assessed": "Read again", "new requirement": "New in this version", "fact used": "Uses the changed fact" };

function show(v: any): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "string" && /^\d{4}-\d{2}-\d{2}T/.test(v)) return fmtDate(v);
  if (Array.isArray(v)) return v.join(", ");
  if (typeof v === "object") return v.amount ? `£${Number(v.amount).toLocaleString("en-GB")}` : JSON.stringify(v);
  return String(v);
}

export default function Changes() {
  usePageTitle("Changes");
  const { profile, refreshChanges } = useProfile();
  const [events, setEvents] = useState<any[] | null>(null);
  const [sel, setSel] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => api<any[]>(`/changes?profile_id=${profile!.id}`).then((e) => {
    setEvents(e); setError(null);
    setSel((s) => (s && e.some((x) => x.id === s) ? s : e.find((x) => x.kind !== "status")?.id ?? e[0]?.id ?? null));
  }).catch((e) => setError(e.message)), [profile?.id]);
  useEffect(() => { setEvents(null); load(); }, [profile?.id]);

  const unfinished = events?.some((e) => e.status === "detected" || e.status === "processing");
  useEffect(() => { if (unfinished) { const t = setTimeout(load, 3000); return () => clearTimeout(t); } }, [events, unfinished, load]);

  const ev = events?.find((e) => e.id === sel);
  const retry = async () => {
    setBusy(true);
    try { await post(`/changes/${ev.id}/retry`, {}); await load(); refreshChanges(); } catch (e: any) { setError(e.message); } finally { setBusy(false); }
  };

  return (
    <>
      <div className="page-head">
        <div>
          <h1>What changed, and what it means for you</h1>
          <p className="lede">When a buyer edits a tender, TenderFit compares the versions and re-checks only what the change touches. Changes sent to you directly are not visible here: upload them on the lot.</p>
        </div>
      </div>
      <ErrorNote error={error} />
      {!events && !error ? <Card><Skeleton /></Card> : events?.length === 0 ? <Card><Empty title="No changes yet">New versions of tenders you assessed appear here after each import.</Empty></Card> : events && (
        <div className="split">
          <div style={{ minWidth: 0 }}>
            {ev?.status && ev.status !== "applied" && ev.kind === "version" && <div className={`banner ${ev.status === "failed" ? "warn" : "info"}`} role={ev.status === "failed" ? "alert" : "status"}>
              {ev.status === "processing" || ev.status === "detected" ? <span className="spinner" aria-hidden /> : <span className="b-icon">!</span>}
              <div className="body">
                <strong>{STATE[ev.status][0]}</strong>
                <p>{ev.status === "failed" ? <>{human(ev.last_error)} Attempt {ev.attempts} failed. Your earlier assessment is still shown on the lot and stays valid for the earlier version.</> :
                  ev.status === "superseded" ? "A later notice replaced this one, so TenderFit compares against that instead." : "The earlier assessment stays in place until this finishes."}</p>
              </div>
              {ev.status === "failed" && <button className="btn primary" disabled={busy} onClick={retry}>Retry now</button>}
            </div>}
            {ev?.kind === "version" && <div className="versions">
              <div className="version"><div className="k">Version {ev.from_version_no ?? "?"}</div><strong>Published {fmtDate(ev.from_published, false)}</strong></div>
              <span aria-hidden>→</span>
              <div className="version current"><div className="k">Version {ev.to_version_no ?? "?"}</div><strong>Edited {fmtDate(ev.to_published, false)}</strong></div>
            </div>}
            {ev?.kind === "version" && <Card flush title="Differences between versions" note={ev.reasons?.length ? ev.reasons[0] : undefined}>
              <div className="table-wrap"><table>
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
              </table></div>
            </Card>}
            {ev?.kind === "profile" && <Card title="A fact in your profile changed"><div>{ev.reasons.join("; ")}</div></Card>}
            {ev?.kind === "status" && <Card title={`${ev.buyer} · ${ev.opportunity}`}><div className="status-bad">{ev.reasons.join("; ")}</div>
              <p className="small muted">Removed from your shortlist; the earlier assessment is archived.</p></Card>}
            {ev?.impacts?.map((imp: any) => <Card key={imp.id} title={`Effect on Lot ${imp.lot_no}${imp.lot_title ? ` · ${imp.lot_title}` : ""}`}
                note={imp.mode === "targeted" ? `${imp.affected.length} re-checked · ${imp.kept} kept` : "Everything re-checked"}>
              {imp.affected.length === 0 && <p className="small muted" style={{ marginTop: 0 }}>Nothing this lot depends on changed in its verdicts.</p>}
              {imp.affected.map((x: any) => (
                <div key={x.requirement_id} style={{ marginBottom: 14 }}>
                  <div style={{ fontWeight: 500 }}>{x.statement}</div>
                  <div className="ba">
                    <div><div className="k">Before</div>{x.before ? <VerdictPill status={x.before} /> : <Tag>New requirement</Tag>}</div>
                    <div className="after"><div className="k">After</div><VerdictPill status={x.after} /><div className="small" style={{ marginTop: 6 }}>{human(x.reason)}</div>
                      <div className="small muted" style={{ marginTop: 2 }}>{HOW[x.how] ?? x.how}</div></div>
                  </div>
                </div>
              ))}
              <div className="ba">
                <div><div className="k">Recommendation before</div>{imp.before_recommendation}</div>
                <div className="after"><div className="k">Recommendation now</div>{imp.after_recommendation}</div>
              </div>
              {imp.audit && <p className="small" style={{ marginBottom: 0 }}>{imp.audit.agrees ? "✓ A full re-check in the background agreed with this result." : "! A full re-check disagreed; TenderFit logged it for review."}</p>}
              <div style={{ marginTop: 12 }}><Link className="btn" to={`/lots/${imp.lot_id}`}>Open updated assessment</Link></div>
            </Card>)}
          </div>
          <div className="sticky">
            {ev?.impacts?.length > 0 && <Card title="Why this was re-checked">
              <p style={{ marginTop: 0 }}>{ev.cls === "local" ? "TenderFit followed what each verdict depends on: the clauses it cites, the fields its rules use, and the deadline where a dated fact is involved. Verdicts that depend on none of the changed items were kept." :
                `The change was too broad to target safely, so every requirement was re-checked. ${ev.reasons?.join("; ") ?? ""}`}</p>
            </Card>}
            <Card title="All changes">
              {events.map((o) => (
                <button key={o.id} className="change-item" aria-current={o.id === sel} onClick={() => setSel(o.id)}>
                  <div className="small muted">{fmtDate(o.detected_at, false)} · <Tag tone={o.status !== "applied" ? STATE[o.status]?.[1] : CLS[o.cls]?.[1]}>{o.status !== "applied" ? STATE[o.status]?.[0] : o.kind === "profile" ? "Profile" : CLS[o.cls]?.[0]}</Tag></div>
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
