import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { usePageTitle, useProfile } from "../App";
import { api, daysLeft, fmtDate } from "../api";
import { Card, Empty, ErrorNote, Fictional, FitPill, Icon, OppStatus, Skeleton, Tag } from "../components/ui";

const money = (v: any) => (v?.amount ? `£${Number(v.amount).toLocaleString("en-GB")}` : null);

export default function Tender() {
  const { tenderId } = useParams();
  const { profile } = useProfile();
  const [t, setT] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  usePageTitle(t?.title ?? "Tender");
  useEffect(() => {
    setT(null); setError(null);
    api(`/tenders/${tenderId}?profile_id=${profile!.id}`).then(setT).catch((e) => setError(e.message));
  }, [tenderId, profile?.id]);
  if (error) return <><div className="crumb"><Link to="/opportunities">Opportunities</Link></div><ErrorNote error={error} /></>;
  if (!t) return <Card><Skeleton lines={6} /></Card>;
  const days = daysLeft(t.deadline);
  const docs = t.documents.filter((d: any) => d.kind !== "notice");
  return (
    <>
      <div className="crumb"><Link to="/opportunities">Opportunities</Link> / {t.buyer}</div>
      <div className="page-head">
        <div style={{ minWidth: 0 }}>
          <div className="title-row"><h1>{t.title}</h1>{t.fictional && <Fictional>Demo notice · scripted AI answers</Fictional>}</div>
          <div className="meta">
            <span>{t.buyer}</span>
            <span>Deadline {fmtDate(t.deadline)}{days !== null && days >= 0 ? ` (${days} days)` : ""}</span>
            <OppStatus status={t.status} suffix={`checked ${fmtDate(t.last_checked_at)}`} />
          </div>
        </div>
        {t.source_url && !t.fictional && <div className="actions"><a className="btn ghost" href={t.source_url} target="_blank" rel="noreferrer"><Icon name="external" size={16} />Source notice</a></div>}
      </div>

      {t.description && <Card title="About this tender"><p className="prose">{t.description}</p>
        <dl className="facts">
          {money(t.value) && <><dt>Estimated value</dt><dd>{money(t.value)}</dd></>}
          {t.procurement_method && <><dt>Procedure</dt><dd>{t.procurement_method}</dd></>}
          {t.regions?.length > 0 && <><dt>Regions</dt><dd className="mono">{t.regions.join(", ")}</dd></>}
          {t.cpv_codes?.length > 0 && <><dt>CPV codes</dt><dd className="mono">{t.cpv_codes.join(", ")}</dd></>}
        </dl></Card>}

      <Card flush title="Lots" note={`${t.lots.length} lot${t.lots.length === 1 ? "" : "s"}`}>
        {t.lots.length === 0 ? <Empty title="No lots in this notice" /> : <ul className="rows">
          {t.lots.map((l: any) => (
            <li key={l.id}><Link className="row-link" style={{ gridTemplateColumns: "minmax(0,1fr) minmax(0,300px) 20px" }} to={`/lots/${l.id}`}>
              <div style={{ minWidth: 0 }}>
                <div className="row-title">Lot {l.lot_no}{l.title ? ` · ${l.title}` : ""}</div>
                <div className="row-sub">{l.description ?? ""}{l.quantity ? ` · quantity ${Number(l.quantity).toLocaleString("en-GB")}` : ""}</div>
                {["awarded", "cancelled", "closed"].includes(l.status) && <Tag tone="muted">{l.status}</Tag>}
              </div>
              <div className="fit-col"><FitPill kind={l.fit?.kind}>{l.fit?.label}</FitPill><div className="fit-reason">{l.fit?.detail}</div></div>
              <span className="chev" aria-hidden>›</span>
            </Link></li>
          ))}
        </ul>}
      </Card>

      <div className="grid-2">
        <Card flush title="Documents" note={docs.length ? `${docs.filter((d: any) => d.status === "read").length} of ${docs.length} read` : undefined}>
          {docs.length === 0 ? <Empty title="No documents listed yet">They are listed when a lot is first opened.</Empty> : <ul className="plain-list">
            {docs.map((d: any) => <li key={d.id}>
              <div style={{ minWidth: 0 }}><div>{d.label}</div><div className="small muted">{d.note ?? ""}</div></div>
              {d.status === "read" ? <Tag tone="ok">✓ Read</Tag> : d.status === "confirmed_none" ? <Tag>Buyer issued none</Tag> : <Tag tone="warn">Not read</Tag>}
            </li>)}
          </ul>}
        </Card>
        <Card flush title="Notice versions" note={`${t.versions.length}`}>
          <ol className="plain-list">
            {t.versions.map((v: any) => <li key={v.id}>
              <div><strong>Version {v.version_no}</strong> <span className="small muted">{v.release_tag === "tenderUpdate" ? "amendment" : "original"}</span></div>
              <span className="small muted">{fmtDate(v.published_at, false)}</span>
            </li>)}
          </ol>
          {t.versions.length > 1 && <div style={{ padding: "0 20px 16px" }}><Link to="/changes">See what changed</Link></div>}
        </Card>
      </div>
    </>
  );
}
