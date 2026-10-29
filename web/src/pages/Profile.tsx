import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { usePageTitle, useProfile } from "../App";
import { api, fmtDate, post, waitForJobs } from "../api";
import { Card, Empty, ErrorNote, EvidencePill, Fictional, Skeleton, Toast } from "../components/ui";

const GROUPS: [string, string[]][] = [
  ["Products and services", ["product", "service"]],
  ["Delivery and capacity", ["region", "capacity", "workload", "staff"]],
  ["Experience", ["experience"]],
  ["Documents and certificates", ["registration", "test_report", "certificate", "insurance", "financial"]],
];

function note(f: any): string | undefined {
  if (f.evidence_status === "needs_update") return f.needs_update_reason ?? undefined;
  if (f.attribute === "insurance" && f.valid_until) return `Expires ${fmtDate(f.valid_until, false)}`;
  if (f.attribute === "capacity" && f.confirmed_at) return `Confirmed ${fmtDate(f.confirmed_at, false)}`;
  if (f.attribute === "experience" && !f.value.reference_letter && f.evidence_status !== "document_on_file") return "No reference letter";
  return undefined;
}

const needsAttention = (f: any) => ["missing", "unknown", "needs_update"].includes(f.evidence_status) ||
  (f.attribute === "experience" && !f.value.reference_letter && f.evidence_status !== "document_on_file");

export default function Profile() {
  const { profile } = useProfile();
  usePageTitle(`Business profile · ${profile?.name ?? ""}`);
  const [facts, setFacts] = useState<any[] | null>(null);
  const [impact, setImpact] = useState<any[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const [target, setTarget] = useState<any>(null);

  const load = () => {
    api<any[]>(`/profile/facts?profile_id=${profile!.id}`).then(setFacts).catch((e) => setError(e.message));
    api<any[]>(`/profiles/${profile!.id}/evidence-impact`).then(setImpact).catch(() => setImpact([]));
  };
  useEffect(load, [profile?.id]);

  const act = async (done: string, fn: () => Promise<any>) => {
    setBusy(true); setError(null); setToast("Saving…");
    try {
      const out = await fn();
      if (out?.jobs?.length) { setToast("Saved. Re-checking affected tenders…"); await waitForJobs(out.jobs); }
      load(); setToast(done);
    } catch (e: any) { setToast(null); setError(e.message); } finally { setBusy(false); }
  };
  const onFile = (file?: File) => {
    if (!file || !target) return;
    const quote = window.prompt(`Copy a sentence from page 1 of ${file.name} that proves "${target.label}". TenderFit checks it is really in the file.`) ?? "";
    const fd = new FormData();
    fd.append("profile_id", String(profile!.id)); fd.append("kind", target.attribute); fd.append("file", file);
    fd.append("fact_id", String(target.id)); fd.append("page", "1"); if (quote) fd.append("quote", quote);
    act("Evidence added.", async () => {
      const out = await api("/profile/evidence", { method: "POST", body: fd });
      if (quote && out.support_verified === false) throw new Error("That sentence is not on page 1 of the file, so the fact was not marked as proven.");
      return out;
    });
  };
  const usedBy = (id: number) => impact.find((i) => i.id === id)?.used_by ?? [];
  const todo = (facts ?? []).filter(needsAttention).sort((a, b) => usedBy(b.id).length - usedBy(a.id).length);

  return (
    <>
      <input type="file" ref={fileRef} hidden accept=".pdf,.docx,.txt" onChange={(e) => { onFile(e.target.files?.[0]); e.target.value = ""; }} />
      <div className="page-head">
        <div>
          <div className="title-row"><h1>Business profile · {profile?.name}</h1>{profile?.is_fictional && <Fictional>Fictional test profile</Fictional>}</div>
          <p className="lede">Every fact shows how well it is supported. Assessments use these facts and never guess.</p>
        </div>
      </div>
      <ErrorNote error={error} />
      {!facts ? <Card><Skeleton lines={6} /></Card> : <>
        <Card title="Needs attention" note={todo.length ? `${todo.length} item${todo.length > 1 ? "s" : ""} · most useful first` : undefined}>
          {todo.length === 0 ? <Empty title="Nothing to fix">Every fact is current and supported.</Empty> : todo.map((f) => (
            <div className="todo" key={f.id}>
              <div style={{ minWidth: 0 }}>
                <div style={{ fontWeight: 500 }}>{f.label}</div>
                <div className="small muted">
                  {usedBy(f.id).length ? <>Affects {usedBy(f.id).map((u: any, i: number) => <span key={i}>{i ? ", " : ""}<Link to={`/lots/${u.lot_id}`}>{u.buyer} · Lot {u.lot_no}</Link></span>)}</>
                    : "Not blocking any assessed tender yet"}
                </div>
              </div>
              <div className="actions">
                <EvidencePill status={f.evidence_status}>{note(f)}</EvidencePill>
                {f.evidence_status === "needs_update"
                  ? <button className="btn sm" disabled={busy} onClick={() => act("Fact re-confirmed.", () => post("/confirmations", { point: "G4", target_id: f.id, decision: "reconfirmed", user: "you", profile_id: profile!.id }))}>Re-confirm</button>
                  : <button className="btn sm" disabled={busy} onClick={() => { setTarget(f); fileRef.current?.click(); }}>Add evidence</button>}
              </div>
            </div>
          ))}
        </Card>
        <div className="grid-2">
          {GROUPS.map(([title, attrs]) => {
            const fs = facts.filter((f) => attrs.includes(f.attribute));
            return (
              <Card key={title} title={title}>
                {fs.length === 0 ? <div className="small muted">Nothing recorded.</div> : fs.map((f) => (
                  <div className="fact" key={f.id}>
                    <div style={{ minWidth: 0 }}>
                      <div>{f.label}</div>
                      {f.supports?.filter((s: any) => s.verified).map((s: any, i: number) => <div className="val" key={i}>▤ {s.filename} · p.{s.page}</div>)}
                    </div>
                    <EvidencePill status={f.evidence_status}>{note(f)}</EvidencePill>
                  </div>
                ))}
              </Card>
            );
          })}
        </div>
        <p className="small muted">Declared capacity is not proof of availability for a given order, so each assessment asks you to confirm current workload.</p>
      </>}
      <Toast message={toast} busy={busy} onDone={() => setToast(null)} />
    </>
  );
}
