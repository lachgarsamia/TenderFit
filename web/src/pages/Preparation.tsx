import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { usePageTitle, useProfile } from "../App";
import { api, fmtDate } from "../api";
import { Card, Empty, ErrorNote, Skeleton, Tag } from "../components/ui";

const STATE: Record<string, [string, string]> = { ready: ["✓ Ready", "ok"], request: ["Request it", "bad"], missing: ["✕ Missing", "bad"], "you prepare": ["You prepare", "muted"] };

export default function Preparation() {
  const { lotId } = useParams();
  const { profile } = useProfile();
  usePageTitle("Bid preparation");
  const [plan, setPlan] = useState<any>(null);
  const [lots, setLots] = useState<any[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [showReady, setShowReady] = useState(false);

  useEffect(() => {
    setPlan(null); setError(null);
    if (lotId) api(`/lots/${lotId}/preparation?profile_id=${profile!.id}`).then(setPlan).catch((e) => setError(e.message));
    else api<any[]>(`/profiles/${profile!.id}/pursued`).then(setLots).catch((e) => setError(e.message));
  }, [lotId, profile?.id]);

  if (!lotId) return (
    <>
      <div className="page-head"><div><h1>Bid preparation</h1><p className="lede">Lots you chose to pursue. TenderFit will not price, commit or submit for you.</p></div></div>
      <ErrorNote error={error} />
      <Card flush>{!lots ? <div style={{ padding: 20 }}><Skeleton /></div> : lots.length === 0 ? (
        <Empty title="No lot pursued yet">Open an assessment and choose Pursue this lot.</Empty>
      ) : <ul className="rows">{lots.map((l) => (
        <li key={l.lot_id}><Link className="row-link" style={{ gridTemplateColumns: "minmax(0,1fr) 220px 20px" }} to={`/lots/${l.lot_id}/preparation`}>
          <div><div className="row-sub">{l.buyer}</div><div className="row-title">{l.title} · Lot {l.lot_no}{l.lot_title ? ` · ${l.lot_title}` : ""}</div></div>
          <div className="small">Deadline {fmtDate(l.deadline)}</div><span className="chev" aria-hidden>›</span>
        </Link></li>))}</ul>}
      </Card>
    </>
  );

  const outstanding = plan?.documents.filter((d: any) => d.state !== "ready") ?? [];
  const ready = plan?.documents.filter((d: any) => d.state === "ready") ?? [];
  return (
    <>
      <div className="crumb"><Link to="/preparation">Bid preparation</Link>{plan ? ` / ${plan.lot.buyer}` : ""}</div>
      <div className="page-head">
        <div>
          <h1>Your preparation plan</h1>
          {plan && <p className="lede">{plan.lot.opportunity} · Lot {plan.lot.lot_no}{plan.lot.title ? ` · ${plan.lot.title}` : ""}. You submit on the buyer's portal; TenderFit never prices or submits.</p>}
        </div>
        <Link className="btn" to={`/lots/${lotId}`}>Back to assessment</Link>
      </div>
      <ErrorNote error={error} />
      {!plan ? <Card><Skeleton lines={5} /></Card> : <>
        {!plan.decision && <div className="banner info"><span className="b-icon">i</span><div className="body"><strong>You have not chosen to pursue this lot yet.</strong><p>This plan is a preview.</p></div></div>}
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
          <Card title="Documents to provide" note={`${outstanding.length} to do · ${ready.length} ready`}>
            {outstanding.length === 0 && <div className="small muted">Nothing outstanding.</div>}
            {outstanding.map((d: any, i: number) => (
              <div className="check-row" key={i}><div><div>{d.label}</div><div className="mono">{d.source}</div></div><Tag tone={STATE[d.state]?.[1]}>{STATE[d.state]?.[0] ?? d.state}</Tag></div>
            ))}
            {ready.length > 0 && <button className="btn ghost sm" style={{ marginTop: 8 }} onClick={() => setShowReady((v) => !v)} aria-expanded={showReady}>
              {showReady ? "Hide" : "Show"} {ready.length} ready</button>}
            {showReady && ready.map((d: any, i: number) => (
              <div className="check-row" key={`r${i}`}><div><div>{d.label}</div><div className="mono">{d.source}</div></div><Tag tone="ok">✓ Ready</Tag></div>
            ))}
          </Card>
          <Card title="Clarification question for the buyer">
            {plan.clarifications.length === 0 ? <div className="small muted">No open point needs the buyer.</div> : plan.clarifications.map((c: any, i: number) => (
              <div key={i}>
                <div className="small muted">Draft from {c.source}. Review it, then send it through the buyer's portal.</div>
                <div className="draft">{c.text}</div>
                <button className="btn primary sm" onClick={() => { navigator.clipboard.writeText(c.text); setCopied(true); }}>{copied ? "✓ Copied" : "Copy question"}</button>
              </div>
            ))}
          </Card>
          <Card title="Confirm inside your company">
            {plan.confirm_inside.length === 0 ? <div className="small muted">Nothing to confirm internally.</div> : plan.confirm_inside.map((c: any, i: number) => (
              <div className="check-row" key={i}><div>{c.label}<div className="mono">{c.source}</div></div><span className="small muted">{c.owner}</span></div>
            ))}
          </Card>
          <Card title="Response sections to write">
            {plan.response_sections.length === 0 ? <div className="small muted">No scored criteria found in the documents read.</div> : plan.response_sections.map((c: any, i: number) => (
              <div className="check-row" key={i}><div>{c.label}</div><span className="mono">{c.source} · scored</span></div>
            ))}
          </Card>
        </div>
      </>}
    </>
  );
}
