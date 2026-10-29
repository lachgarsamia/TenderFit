import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useProfile } from "../App";
import { api, fmtDate, post, waitForJobs } from "../api";
import { Card, Empty, ErrorNote, EvidenceBadge, Fictional } from "../components/ui";

const GROUPS: [string, string[]][] = [
  ["Products and services", ["product", "service"]],
  ["Delivery and capacity", ["region", "capacity", "workload", "staff"]],
  ["Experience", ["experience"]],
  ["Documents and certificates", ["registration", "test_report", "certificate", "insurance", "financial"]],
];

function badgeText(f: any): string | undefined {
  if (f.evidence_status === "needs_update") return f.needs_update_reason ? `Needs update · ${f.needs_update_reason}` : undefined;
  if (f.attribute === "insurance" && f.valid_until) return `Expires ${fmtDate(f.valid_until, false)}`;
  if (f.attribute === "capacity" && f.confirmed_at) return `Confirmed ${fmtDate(f.confirmed_at, false)}`;
  if (f.attribute === "experience" && !f.value.reference_letter && f.evidence_status !== "document_on_file") return "No reference";
  if (f.evidence_status === "document_on_file" && f.supports?.[0]?.filename?.includes("spec")) return "Spec sheet";
  if (f.evidence_status === "document_on_file") return "On file";
  return undefined;
}

export default function Profile() {
  const { profile } = useProfile();
  const [facts, setFacts] = useState<any[] | null>(null);
  const [impact, setImpact] = useState<any[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const [target, setTarget] = useState<any>(null);

  const load = () => {
    api<any[]>(`/profile/facts?profile_id=${profile!.id}`).then(setFacts).catch((e) => setError(e.message));
    api<any[]>(`/profiles/${profile!.id}/evidence-impact`).then(setImpact).catch(() => setImpact([]));
  };
  useEffect(load, [profile?.id]);

  const act = async (fn: () => Promise<any>) => {
    setBusy(true); setError(null);
    try { const out = await fn(); if (out?.jobs?.length) await waitForJobs(out.jobs); load(); }
    catch (e: any) { setError(e.message); } finally { setBusy(false); }
  };
  const reconfirm = (f: any) => act(() => post("/confirmations", { point: "G4", target_id: f.id, decision: "reconfirmed", user: "you", profile_id: profile!.id }));
  const onFile = (file?: File) => {
    if (!file || !target) return;
    const quote = window.prompt(`Quote from page 1 of ${file.name} that supports "${target.label}" (checked against the file):`) ?? "";
    const fd = new FormData();
    fd.append("profile_id", String(profile!.id)); fd.append("kind", target.attribute); fd.append("file", file);
    fd.append("fact_id", String(target.id)); fd.append("page", "1"); if (quote) fd.append("quote", quote);
    act(async () => {
      const out = await api("/profile/evidence", { method: "POST", body: fd });
      if (quote && out.support_verified === false) throw new Error("The quote was not found on page 1 of that file, so the fact was not marked as backed by a document.");
      return out;
    });
  };

  return (
    <>
      <input type="file" ref={fileRef} hidden accept=".pdf,.docx,.txt" onChange={(e) => onFile(e.target.files?.[0])} />
      <div className="head-row">
        <div>
          <div className="title-row"><h1>Business profile · {profile?.name}</h1>{profile?.is_fictional && <Fictional>Fictional test profile</Fictional>}</div>
          <p className="sub">Every fact shows how well it is supported. Assessments use this, and never guess.</p>
        </div>
      </div>
      <div className="legend">
        <EvidenceBadge status="document_on_file" /><EvidenceBadge status="declared" /><EvidenceBadge status="needs_update" />
        <EvidenceBadge status="missing">Evidence missing</EvidenceBadge><EvidenceBadge status="unknown" />
      </div>
      <ErrorNote error={error} />
      {!facts ? <Empty>Loading…</Empty> : (
        <div className="grid-main">
          <div className="grid-2">
            {GROUPS.map(([title, attrs]) => {
              const fs = facts.filter((f) => attrs.includes(f.attribute));
              return (
                <Card key={title} title={title}>
                  {fs.length === 0 ? <div className="small muted">Nothing recorded.</div> : fs.map((f) => (
                    <div className="fact" key={f.id}>
                      <div>
                        <div>{f.label}</div>
                        {f.attribute === "capacity" && <div className="val">Declared capacity is not proof of availability for a given order, so each assessment asks you to confirm current workload.</div>}
                        {f.supports?.filter((s: any) => s.verified).map((s: any, i: number) => <div className="val" key={i}>▤ {s.filename} · p.{s.page}</div>)}
                      </div>
                      <div style={{ textAlign: "right" }}>
                        <EvidenceBadge status={f.evidence_status}>{badgeText(f)}</EvidenceBadge>
                        <div style={{ marginTop: 4 }}>
                          {f.evidence_status === "needs_update" && <button className="btn sm" disabled={busy} onClick={() => reconfirm(f)}>Re-confirm</button>}
                          {["missing", "unknown", "declared"].includes(f.evidence_status) &&
                            <button className="btn sm" disabled={busy} onClick={() => { setTarget(f); fileRef.current?.click(); }}>Upload evidence</button>}
                        </div>
                      </div>
                    </div>
                  ))}
                </Card>
              );
            })}
          </div>
          <Card title="Where missing evidence matters">
            <div className="small muted" style={{ marginBottom: 10 }}>Shortlisted opportunities that depend on each missing or unconfirmed item.</div>
            {impact.length === 0 ? <div className="small muted">No assessed opportunity is waiting on missing evidence.</div> : impact.map((f) => (
              <div className="impact" key={f.id}>
                <div style={{ fontWeight: 600 }}>{f.label}</div>
                {f.used_by.map((u: any, i: number) => (
                  <div className="small muted" key={i}><Link to={`/lots/${u.lot_id}`}>{u.buyer} · Lot {u.lot_no}</Link> · {u.obligation}</div>
                ))}
              </div>
            ))}
            {impact.length > 0 && <div className="small" style={{ color: "var(--navy)", fontWeight: 500 }}>
              {impact[0].label} would close a gap in {impact[0].used_by.length} opportunit{impact[0].used_by.length === 1 ? "y" : "ies"}.</div>}
          </Card>
        </div>
      )}
    </>
  );
}
