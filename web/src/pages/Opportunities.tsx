import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { usePageTitle, useProfile } from "../App";
import { daysLeft, fmtDate, post } from "../api";
import { Card, Empty, ErrorNote, Fictional, FitPill, Icon, OppStatus, Skeleton, Tabs, Tag } from "../components/ui";

const REGION: Record<string, string> = { UKC: "North East", UKD: "North West", UKE: "Yorkshire & Humber", UKF: "East Midlands",
  UKG: "West Midlands", UKH: "East of England", UKI: "London", UKJ: "South East", UKK: "South West", UKL: "Wales", UKM: "Scotland", UKN: "Northern Ireland" };
const CPV: Record<string, string> = { "39": "Furnishings", "391": "Furniture", "3911": "Seating", "3912": "Storage", "3916": "School furniture" };
const RANK: Record<string, number> = { blocker: 0, provisional: 1, relevant: 2, unassessed: 3, low: 4 };
type Bucket = "assessed" | "to_assess" | "low" | "closed";

function bucket(o: any): Bucket {
  if (o.lots.some((l: any) => l.fit?.assessed)) return "assessed";
  return o.lots.every((l: any) => l.fit?.kind === "low") ? "low" : "to_assess";
}

function bestLot(o: any) {
  return [...o.lots].sort((a, b) => (b.fit?.assessed ? 1 : 0) - (a.fit?.assessed ? 1 : 0) || (RANK[a.fit?.kind] ?? 9) - (RANK[b.fit?.kind] ?? 9))[0];
}

export default function Opportunities() {
  usePageTitle("Opportunities");
  const { profile } = useProfile();
  const regions = (profile?.regions ?? []).map((r) => REGION[r] ?? r);
  const defaultQ = `Furniture tenders we could deliver in ${regions.join(", ")}`;
  const [q, setQ] = useState(defaultQ);
  const [data, setData] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<Bucket | null>(null);

  const run = async (query = q) => {
    setBusy(true); setError(null);
    try { setData(await post("/search", { q: query, profile_id: profile!.id, limit: 40 })); }
    catch (e: any) { setError(e.message); } finally { setBusy(false); }
  };
  useEffect(() => { setQ(defaultQ); setTab(null); run(defaultQ); }, [profile?.id]);

  const groups = useMemo(() => {
    const g: Record<Bucket, any[]> = { assessed: [], to_assess: [], low: [], closed: data?.no_longer_open ?? [] };
    for (const o of data?.shortlist ?? []) g[bucket(o)].push(o);
    g.assessed.sort((a, b) => (RANK[bestLot(a).fit?.kind] ?? 9) - (RANK[bestLot(b).fit?.kind] ?? 9));
    return g;
  }, [data]);
  const active: Bucket = tab ?? (groups.assessed.length ? "assessed" : "to_assess");
  const list = groups[active];
  const p = data?.parsed;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Opportunities</h1>
          <p className="lede">Open UK public tenders matched to {profile?.name}, with the reason behind every match.</p>
        </div>
      </div>

      <Card>
        <form className="search" onSubmit={(e) => { e.preventDefault(); run(); }} role="search">
          <label className="search-input">
            <Icon name="search" />
            <span className="sr-only">Describe what you are looking for</span>
            <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="e.g. school furniture in Yorkshire" />
          </label>
          <button className="btn primary" disabled={busy}>{busy ? "Searching…" : "Search"}</button>
        </form>
        {p && (
          <div className="filters">
            <span>Showing</span>
            <span className="chip">{p.cpv.length ? p.cpv.map((c: string) => CPV[c] ?? `CPV ${c}`).join(", ") : "All sectors"}</span>
            <span className="chip">{p.regions.length ? p.regions.map((r: string) => REGION[r] ?? r).join(", ") : "All UK regions"}</span>
            <span className="chip">Open tenders</span>
          </div>
        )}
        <div className="source-line">↻ Live from Find a Tender · updated {fmtDate(data?.last_import)}</div>
      </Card>

      <ErrorNote error={error} />

      <Card flush>
        <Tabs label="Opportunity groups" value={active} onChange={setTab} tabs={[
          { id: "assessed", label: "Assessed", count: groups.assessed.length },
          { id: "to_assess", label: "To assess", count: groups.to_assess.length },
          { id: "low", label: "Low relevance", count: groups.low.length },
          { id: "closed", label: "No longer open", count: groups.closed.length },
        ]} />
        {!data ? <div style={{ padding: "8px 20px" }}><Skeleton /></div> : list.length === 0 ? (
          <Empty title={{ assessed: "Nothing assessed yet", to_assess: "No new matches", low: "No low-relevance matches", closed: "Nothing has closed" }[active]}>
            {active === "assessed" ? "Open a tender under To assess and choose Assess this lot." : "Try a broader search."}
          </Empty>
        ) : (
          <ul className="rows">
            {list.map((o) => active === "closed" ? (
              <li key={o.id}><div className="row-link" style={{ cursor: "default" }}>
                <div><div className="row-sub">{o.buyer}</div><div className="row-title">{o.title}</div></div>
                <div className="when-col"><div className="strike small">{fmtDate(o.deadline)}</div><OppStatus status={o.status} /></div>
                <div className="fit-col small muted">{o.status_reason}. Earlier assessment archived.</div><span />
              </div></li>
            ) : <Row key={o.id} o={o} />)}
          </ul>
        )}
      </Card>
    </>
  );
}

function Row({ o }: { o: any }) {
  const lot = bestLot(o);
  const days = daysLeft(o.deadline);
  return (
    <li>
      <Link className="row-link" to={`/lots/${lot.id}`}>
        <div style={{ minWidth: 0 }}>
          <div className="row-sub">{o.buyer}{o.lots.length > 1 ? ` · ${o.lots.length} lots` : ""} {o.fictional && <Fictional />}</div>
          <div className="row-title">{o.title}</div>
          {o.lots.length > 1 && (
            <div className="row-lots">
              {o.lots.map((l: any) => (
                <span key={l.id} className="muted">Lot {l.lot_no}{l.title ? ` · ${l.title}` : ""} · <FitPill kind={l.fit?.kind}>{l.fit?.label}</FitPill></span>
              ))}
            </div>
          )}
        </div>
        <div className="when-col">
          <div>{fmtDate(o.deadline)}</div>
          <div className="small"><OppStatus status={o.status} suffix={days !== null && days >= 0 ? `${days} days left` : undefined} /></div>
          {o.versions > 1 && <div style={{ marginTop: 4 }}><Tag tone="warn">Amended · v{o.versions}</Tag></div>}
        </div>
        <div className="fit-col">
          <FitPill kind={lot.fit?.kind}>{lot.fit?.label}</FitPill>
          <div className="fit-reason">{lot.fit?.detail}</div>
        </div>
        <span className="chev" aria-hidden>›</span>
      </Link>
    </li>
  );
}
