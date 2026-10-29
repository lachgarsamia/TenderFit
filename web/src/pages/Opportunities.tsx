import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useProfile } from "../App";
import { daysLeft, fmtDate, post } from "../api";
import { Card, Empty, ErrorNote, FitBadge, Fictional, OppStatus, Tag } from "../components/ui";

const REGION_NAMES: Record<string, string> = { UKC: "North East", UKD: "North West", UKE: "Yorkshire & Humber", UKF: "East Midlands",
  UKG: "West Midlands", UKH: "East of England", UKI: "London", UKJ: "South East", UKK: "South West", UKL: "Wales", UKM: "Scotland", UKN: "Northern Ireland" };

export default function Opportunities() {
  const { profile } = useProfile();
  const nav = useNavigate();
  const defaultQ = `Find furniture tenders we could deliver in ${(profile?.regions ?? []).map((r) => REGION_NAMES[r] ?? r).join(" or ")}, and tell me which deserve a closer look.`;
  const [q, setQ] = useState(defaultQ);
  const [data, setData] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = async (query = q) => {
    setBusy(true); setError(null);
    try { setData(await post("/search", { q: query, profile_id: profile!.id, min_days: 0, limit: 25 })); }
    catch (e: any) { setError(e.message); } finally { setBusy(false); }
  };
  useEffect(() => { setQ(defaultQ); run(defaultQ); }, [profile?.id]);

  const parsed = data?.parsed;
  return (
    <>
      <div className="title-row"><h1>Opportunities</h1><Tag tone="info">Live data · Find a Tender (UK)</Tag></div>
      <p className="sub">Open public tenders matched to your profile, with the reason behind every match.</p>
      <Card>
        <div className="ask-head">
          <span>Ask TenderFit</span>
          <span>↻ Find a Tender (UK) · last import {fmtDate(data?.last_import)} · status re-checked on every import</span>
        </div>
        <form className="ask" onSubmit={(e) => { e.preventDefault(); run(); }}>
          <input value={q} onChange={(e) => setQ(e.target.value)} aria-label="Describe what you are looking for" />
          <button className="btn primary" disabled={busy}>{busy ? "Searching…" : "Investigate"}</button>
        </form>
        {parsed && (
          <div className="chips">
            {parsed.cpv.length > 0 && <span className="chip">CPV {parsed.cpv.join(", ")}</span>}
            {parsed.regions.length > 0 && <span className="chip">Delivery: {parsed.regions.map((r: string) => REGION_NAMES[r] ?? r).join(", ")}</span>}
            <span className="chip">Open or unverified only</span>
            <span className="chip">Tender notices (UK4) incl. below threshold</span>
          </div>
        )}
      </Card>
      <ErrorNote error={error} />
      <Card title="Shortlist" extra={data && `${data.shortlist.length} open candidates · ${data.no_longer_open.length} no longer open`}>
        {!data ? <Empty>Loading…</Empty> : data.shortlist.length === 0 ? <Empty>No open opportunities match. Try fewer filters.</Empty> : (
          <table>
            <thead><tr><th>Opportunity</th><th style={{ width: 210 }}>Deadline · status</th><th>Fit for {profile?.name}</th><th></th></tr></thead>
            <tbody>
              {data.shortlist.map((o: any) => {
                const days = daysLeft(o.deadline);
                return o.lots.map((l: any, i: number) => (
                  <tr key={l.id}>
                    <td>
                      {i === 0 && <div className="small muted">{o.buyer} · {o.lots.length} lot{o.lots.length > 1 ? "s" : ""} {o.fictional && <Fictional>Fictional · scripted model</Fictional>}</div>}
                      <div style={{ fontWeight: 600 }}>{i === 0 ? o.title : ""}</div>
                      {o.lots.length > 1 && <div className="small">Lot {l.lot_no}{l.title ? ` · ${l.title}` : ""}</div>}
                    </td>
                    <td>
                      {i === 0 && <>
                        <div>{fmtDate(o.deadline)}</div>
                        <div className="small"><OppStatus status={o.status} suffix={days !== null && days >= 0 ? `${days} days left` : undefined} /></div>
                        {o.versions > 1 && <div style={{ marginTop: 4 }}><Tag tone="warn">Amended {fmtDate(o.last_version_at, false)} · v{o.versions}</Tag></div>}
                        {o.status === "unverified" && <div className="small warn-text">{o.status_reason}</div>}
                      </>}
                    </td>
                    <td>
                      {l.fit && <>
                        <FitBadge kind={l.fit.kind}>{l.fit.label}</FitBadge>
                        <div className="small muted" style={{ marginTop: 4 }}>
                          {l.fit.detail}
                        </div>
                      </>}
                    </td>
                    <td style={{ textAlign: "right" }}>
                      <button className={`btn ${l.fit?.assessed ? "primary" : ""}`} onClick={() => nav(`/lots/${l.id}`)}>
                        {l.fit?.assessed ? "Open assessment" : l.fit?.kind === "low" ? "See why" : "Assess"}
                      </button>
                    </td>
                  </tr>
                ));
              })}
            </tbody>
          </table>
        )}
        {data?.no_longer_open.length > 0 && <>
          <div className="section-label">No longer open · hidden from shortlist</div>
          <table><tbody>
            {data.no_longer_open.map((o: any) => (
              <tr key={o.id}>
                <td><div className="small muted">{o.buyer}</div><div style={{ fontWeight: 600 }}>{o.title}</div></td>
                <td><div className="strike">{fmtDate(o.deadline)}</div><OppStatus status={o.status} /></td>
                <td className="small muted">{o.status_reason}; earlier assessment archived.</td><td></td>
              </tr>
            ))}
          </tbody></table>
        </>}
      </Card>
    </>
  );
}
