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
const LADDER: [string, string][] = [["exists", "File on record"], ["relevant", "Names the subject"], ["supports", "Supports the claim"], ["current", "Current"]];

function note(f: any): string | undefined {
  if (f.evidence_status === "needs_update") return f.needs_update_reason ?? undefined;
  if (f.attribute === "insurance" && f.valid_until) return `Expires ${fmtDate(f.valid_until, false)}`;
  if (f.attribute === "capacity" && f.confirmed_at) return `Confirmed ${fmtDate(f.confirmed_at, false)}`;
  if (f.attribute === "experience" && !f.value.reference_letter && f.evidence_status !== "document_on_file") return "No reference letter";
  return undefined;
}

const expired = (f: any) => !!f.valid_until && new Date(f.valid_until) < new Date();
const needsAttention = (f: any) => ["missing", "unknown", "needs_update"].includes(f.evidence_status) || expired(f) ||
  (f.attribute === "experience" && !f.value.reference_letter && f.evidence_status !== "document_on_file");

function Verification({ f }: { f: any }) {
  const v = f.verification;
  if (!v) return null;
  const at = LADDER.findIndex(([k]) => k === v.level);
  const checks = Object.values(v.checks ?? {}) as any[];
  return (
    <details className="verification">
      <summary>Checked: {LADDER[at]?.[1] ?? v.level}{f.supports?.some((s: any) => s.human_verified_by) ? " · confirmed by a person" : ""}</summary>
      <ol className="ladder">{LADDER.map(([k, label], i) => <li key={k} className={i <= at ? "done" : ""}><span aria-hidden>{i <= at ? "✓" : "○"}</span> {label}</li>)}
        <li className={f.supports?.some((s: any) => s.human_verified_by) ? "done" : ""}><span aria-hidden>{f.supports?.some((s: any) => s.human_verified_by) ? "✓" : "○"}</span> Confirmed by a person</li></ol>
      <ul className="check-list">{checks.map((c, i) => <li key={i} className={`c-${c.result}`}>{c.detail}</li>)}</ul>
      <p className="small muted">These checks read the file's text only. They do not establish that a document is genuine.</p>
    </details>
  );
}

function EvidenceDialog({ fact, renew, onClose, onSubmit, busy, error }: { fact: any; renew: boolean; onClose: () => void; onSubmit: (d: FormData) => void; busy: boolean; error: string | null }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => { ref.current?.showModal(); }, []);
  return (
    <dialog ref={ref} className="dialog" onClose={onClose} aria-labelledby="ev-title">
      <form onSubmit={(e) => { e.preventDefault(); onSubmit(new FormData(e.currentTarget)); }}>
        <header className="dialog-head"><div><h2 id="ev-title">{renew ? "Renew evidence" : "Add evidence"}</h2><div className="small muted">{fact.label}</div></div>
          <button type="button" className="btn ghost sm" onClick={() => ref.current?.close()}>Close</button></header>
        <ErrorNote error={error} />
        <div className="field"><label htmlFor="ev-file">Document (PDF, DOCX or TXT, up to 25 MB)</label><input id="ev-file" name="file" type="file" accept=".pdf,.docx,.txt" required /></div>
        <div className="field"><label htmlFor="ev-quote">A sentence from the document that proves this</label>
          <textarea id="ev-quote" name="quote" required minLength={12} placeholder="Copy it exactly. TenderFit checks it is really in the file." /></div>
        <div className="field-row">
          <div className="field"><label htmlFor="ev-page">Page</label><input id="ev-page" name="page" type="number" min={1} defaultValue={1} required /></div>
          <div className="field"><label htmlFor="ev-exp">Expiry date{renew ? "" : " (if it has one)"}</label><input id="ev-exp" name="expires_on" type="date" required={renew} /></div>
          <div className="field"><label htmlFor="ev-iss">Issued by</label><input id="ev-iss" name="issuer" type="text" /></div>
        </div>
        <footer className="dialog-foot"><button className="btn primary" disabled={busy}>{busy ? "Checking…" : renew ? "Renew and re-check" : "Add and check"}</button></footer>
      </form>
    </dialog>
  );
}

export default function Profile() {
  const { profile } = useProfile();
  usePageTitle(`Business profile · ${profile?.name ?? ""}`);
  const [facts, setFacts] = useState<any[] | null>(null);
  const [impact, setImpact] = useState<any[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const [dialog, setDialog] = useState<{ fact: any; renew: boolean } | null>(null);

  const load = async () => {
    try {
      const [f, i] = await Promise.all([api<any[]>(`/profile/facts?profile_id=${profile!.id}`), api<any[]>(`/profiles/${profile!.id}/evidence-impact`)]);
      setFacts(f); setImpact(i); setError(null);
    } catch (e: any) { setError(e.message); }
  };
  useEffect(() => { setFacts(null); load(); }, [profile?.id]);

  const act = async (done: string, fn: () => Promise<any>) => {
    setBusy(true); setError(null); setToast("Saving…");
    try {
      const out = await fn();
      if (out?.jobs?.length) { setToast("Saved. Re-checking affected tenders…"); await waitForJobs(out.jobs); }
      await load(); setToast(done);
    } catch (e: any) { setToast(null); setError(e.message); } finally { setBusy(false); }
  };
  const submit = async (d: FormData) => {
    if (!dialog) return;
    const { fact, renew } = dialog;
    d.append("profile_id", String(profile!.id));
    if (!(d.get("expires_on") as string)) d.delete("expires_on");
    if (!(d.get("issuer") as string)) d.delete("issuer");
    if (!renew) { d.append("kind", fact.attribute); d.append("fact_id", String(fact.id)); }
    setBusy(true); setFormError(null);
    try {
      const out = await api(renew ? `/profile/facts/${fact.id}/renew` : "/profile/evidence", { method: "POST", body: d });
      const ok = renew ? out.verified : out.support_verified;
      if (!ok) { setFormError(renew ? "The file did not back this up: the sentence was not found on that page, or the file contradicts the fact." : "That sentence is not on that page, or the file contradicts the fact, so the fact was not marked as proven."); await load(); return; }
      setDialog(null); setToast(out.jobs?.length ? "Saved. Re-checking affected tenders…" : "Evidence checked and added.");
      if (out.jobs?.length) await waitForJobs(out.jobs);
      await load(); setToast("Evidence checked and added.");
    } catch (e: any) { setFormError(e.message); } finally { setBusy(false); }
  };
  const usedBy = (id: number) => impact.find((i) => i.id === id)?.used_by ?? [];
  const todo = (facts ?? []).filter(needsAttention).sort((a, b) => usedBy(b.id).length - usedBy(a.id).length);
  const open = (fact: any, renew: boolean) => { setFormError(null); setDialog({ fact, renew }); };

  return (
    <>
      <div className="page-head">
        <div>
          <div className="title-row"><h1>Business profile · {profile?.name}</h1>{profile?.is_fictional && <Fictional>Fictional test profile</Fictional>}</div>
          <p className="lede">Every fact shows how well it is supported. A file on record is not proof by itself: the check below says what its text actually shows.</p>
        </div>
      </div>
      <ErrorNote error={error} />
      {!facts ? (!error && <Card><Skeleton lines={6} /></Card>) : <>
        <Card title="Needs attention" note={todo.length ? `${todo.length} item${todo.length > 1 ? "s" : ""} · most useful first` : undefined}>
          {todo.length === 0 ? <Empty title="Nothing to fix">Every fact is current and supported.</Empty> : todo.map((f) => {
            const dated = f.evidence_status === "needs_update" || expired(f);
            return (
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
                {dated && !expired(f) && <button className="btn sm" disabled={busy} onClick={() => act("Fact re-confirmed.", () => post("/confirmations", { point: "G4", target_id: f.id, decision: "reconfirmed", user: "you", profile_id: profile!.id }))}>Re-confirm</button>}
                {dated ? <button className="btn sm primary" disabled={busy} onClick={() => open(f, true)}>Renew</button> : <button className="btn sm" disabled={busy} onClick={() => open(f, false)}>Add evidence</button>}
              </div>
            </div>
          ); })}
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
                      <Verification f={f} />
                      {f.supports?.some((s: any) => s.verified && !s.human_verified_by) && <button className="btn ghost sm" style={{ marginTop: 6 }} disabled={busy}
                        onClick={() => act("Recorded: you checked this document yourself.", () => post(`/profile/facts/${f.id}/verify`, { profile_id: profile!.id, user: "you" }))}>I have checked this document</button>}
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
      {dialog && <EvidenceDialog fact={dialog.fact} renew={dialog.renew} busy={busy} error={formError} onClose={() => setDialog(null)} onSubmit={submit} />}
      <Toast message={toast} busy={busy} onDone={() => setToast(null)} />
    </>
  );
}
