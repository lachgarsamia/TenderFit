import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useProfile } from "../App";
import { api, fmtDate } from "../api";
import { Card, Empty, ErrorNote, Tag } from "../components/ui";

const STATE: Record<string, [string, string]> = { ready: ["Ready", "ok"], request: ["Request it", "bad"], missing: ["Missing", "bad"], "you prepare": ["You prepare", "muted"] };

export default function Preparation() {
  const { lotId } = useParams();
  const { profile } = useProfile();
  const [plan, setPlan] = useState<any>(null);
  const [lots, setLots] = useState<any[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    setPlan(null); setError(null);
    if (lotId) api(`/lots/${lotId}/preparation?profile_id=${profile!.id}`).then(setPlan).catch((e) => setError(e.message));
    else api<any[]>(`/profiles/${profile!.id}/pursued`).then(setLots).catch((e) => setError(e.message));
  }, [lotId, profile?.id]);

  if (!lotId) return (
    <>
      <h1>Bid preparation</h1>
      <p className="sub">Lots you chose to pursue. TenderFit will not price, commit or submit for you.</p>
      <ErrorNote error={error} />
      <Card>{!lots ? <Empty>Loading…</Empty> : lots.length === 0 ? <Empty>No lot pursued yet. Open an assessment and choose "Pursue this lot".</Empty> : lots.map((l) => (
        <div className="row" key={l.lot_id}>
          <div><div className="small muted">{l.buyer}</div><div style={{ fontWeight: 600 }}>{l.title} · Lot {l.lot_no}{l.lot_title ? ` · ${l.lot_title}` : ""}</div></div>
          <div className="actions"><span className="small muted">Deadline {fmtDate(l.deadline)}</span><Link className="btn primary" to={`/lots/${l.lot_id}/preparation`}>Open plan</Link></div>
        </div>
      ))}</Card>
    </>
  );

  return (
    <>
      <div className="crumb"><Link to="/preparation">Bid preparation</Link>{plan ? ` / ${plan.lot.buyer} · Lot ${plan.lot.lot_no}${plan.lot.title ? ` · ${plan.lot.title}` : ""}` : ""}</div>
      <h1>Your preparation plan</h1>
      <ErrorNote error={error} />
      {!plan ? <Empty>Loading…</Empty> : <>
        <p className="sub">{plan.decision ? `You chose to pursue this lot on ${fmtDate(plan.decision.created_at, false)}. ` : "You have not chosen to pursue this lot yet. "}{plan.note}</p>
        <Card>
          <div className="timeline">
            {plan.milestones.map((m: any, i: number) => (
              <div key={i} className={`milestone ${m.done ? "done" : ""} ${m.final ? "final" : ""}`}>
                <div className="d">{m.final ? fmtDate(m.date) : `${m.done ? "" : "by "}${fmtDate(m.date, false)}${m.done ? " · done" : ""}`}</div>
                <div className="l">{m.label}</div>
                {m.note && <div className="small muted">{m.note}</div>}
              </div>
            ))}
          </div>
        </Card>
        <div className="grid-2">
          <Card title="Documents to provide">
            {plan.documents.length === 0 ? <div className="small muted">Nothing outstanding.</div> : plan.documents.map((d: any, i: number) => (
              <div className="row" key={i}><div><div>{d.label}</div><div className="mono">{d.source}</div></div>
                <Tag tone={STATE[d.state]?.[1]}>{STATE[d.state]?.[0] ?? d.state}</Tag></div>
            ))}
          </Card>
          <Card title="Clarification question for the buyer">
            {plan.clarifications.length === 0 ? <div className="small muted">No open point that needs the buyer.</div> : plan.clarifications.map((c: any, i: number) => (
              <div key={i}>
                <div className="small muted">Draft based on {c.source}. Review it before sending through the buyer's portal.</div>
                <div className="draft">{c.text}</div>
                <button className="btn primary sm" onClick={() => { navigator.clipboard.writeText(c.text); setCopied(true); }}>{copied ? "Copied" : "Copy question"}</button>
              </div>
            ))}
          </Card>
          <Card title="Confirm inside your company">
            {plan.confirm_inside.length === 0 ? <div className="small muted">Nothing to confirm internally.</div> : plan.confirm_inside.map((c: any, i: number) => (
              <div className="row" key={i}><div>{c.label}<div className="mono">{c.source}</div></div><span className="small muted">{c.owner}</span></div>
            ))}
          </Card>
          <Card title="Response sections to write">
            {plan.response_sections.length === 0 ? <div className="small muted">No scored criteria were found in the documents read.</div> : plan.response_sections.map((c: any, i: number) => (
              <div className="row" key={i}><div>{c.label}</div><span className="mono">{c.source} · scored</span></div>
            ))}
          </Card>
        </div>
        <Link className="btn" to={`/lots/${lotId}`}>← Back to the assessment</Link>
      </>}
    </>
  );
}
