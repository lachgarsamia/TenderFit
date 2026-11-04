import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { ProfileFields, profileBody } from "./Account";
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

type Field = { key: string; label: string; type?: "text" | "number" | "date" | "list" | "check" | "select"; options?: [string, string][] };
const FACT_KINDS: Record<string, { title: string; fields: Field[]; dated?: boolean }> = {
  product: { title: "Product", fields: [{ key: "name", label: "Product name" }, { key: "code", label: "Product code" }, { key: "cpv", label: "CPV codes (comma separated)", type: "list" }] },
  service: { title: "Service", fields: [{ key: "service", label: "Service, for example installation" }] },
  region: { title: "Delivery regions", fields: [{ key: "itl", label: "ITL codes (comma separated)", type: "list" }] },
  capacity: { title: "Capacity", fields: [{ key: "scope", label: "What it applies to, for example desks" }, { key: "max_units", label: "Most units you can supply", type: "number" }, { key: "lead_days", label: "Lead time in days", type: "number" }] },
  experience: { title: "Past contract", fields: [{ key: "description", label: "What you delivered" }, { key: "units", label: "Number of items", type: "number" }, { key: "end", label: "End date", type: "date" }, { key: "reference_letter", label: "I have a customer reference", type: "check" }] },
  certificate: { title: "Certificate", dated: true, fields: [{ key: "standard", label: "Standard, for example ISO 9001" }, { key: "scope", label: "Scope of certification" }] },
  test_report: { title: "Test report", dated: true, fields: [{ key: "standard", label: "Standard, for example BS EN 1729-2" }, { key: "product", label: "Product tested" }] },
  insurance: { title: "Insurance", dated: true, fields: [{ key: "type", label: "Policy type", type: "select", options: [["public_liability", "Public liability"], ["employers_liability", "Employers liability"], ["professional_indemnity", "Professional indemnity"], ["product_liability", "Product liability"]] }, { key: "amount_gbp", label: "Cover in pounds", type: "number" }] },
  registration: { title: "Company registration", fields: [{ key: "company_number", label: "Company number" }] },
};

function readValue(kind: string, d: FormData): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const f of FACT_KINDS[kind].fields) {
    const raw = d.get(`v_${f.key}`);
    if (f.type === "check") out[f.key] = raw === "on";
    else if (f.type === "number") { if (raw) out[f.key] = Number(raw); }
    else if (f.type === "list") { const l = String(raw ?? "").split(",").map((x) => x.trim()).filter(Boolean); if (l.length) out[f.key] = l; }
    else if (raw) out[f.key] = String(raw);
  }
  return out;
}

function FactDialog({ fact, onClose, onSaved }: { fact: any | null; onClose: () => void; onSaved: (message: string, jobs: number[]) => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  const [kind, setKind] = useState<string>(fact?.attribute ?? "certificate");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { ref.current?.showModal(); }, []);
  const spec = FACT_KINDS[kind];
  const submit = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const d = new FormData(e.currentTarget);
    const base: any = { label: d.get("label"), value: readValue(kind, d), evidence_status: fact?.evidence_status === "document_on_file" ? undefined : d.get("status") };
    const until = d.get("valid_until") as string | null;
    setBusy(true); setError(null);
    try {
      const out = fact
        ? await api(`/profile/facts/${fact.id}`, { method: "PUT", body: JSON.stringify({ ...base, valid_until: until || undefined, clear_expiry: spec.dated && !until }) })
        : await post("/profile/facts", { ...base, attribute: kind, valid_until: until || undefined });
      onSaved(out.detached_from_documents ? "Saved. The value changed, so the documents that proved the old value no longer count: add evidence again." : "Saved.", out.jobs ?? []);
    } catch (err: any) { setError(err.message); setBusy(false); }
  };
  return (
    <dialog ref={ref} className="dialog" onClose={onClose} aria-labelledby="fact-title">
      <form onSubmit={submit}>
        <header className="dialog-head"><div><h2 id="fact-title">{fact ? "Edit fact" : "Add a fact"}</h2><div className="small muted">What you enter counts as declared until a checked document supports it.</div></div>
          <button type="button" className="btn ghost sm" onClick={() => ref.current?.close()}>Close</button></header>
        <ErrorNote error={error} />
        {!fact && <div className="field"><label htmlFor="f-kind">Kind of fact</label>
          <select id="f-kind" value={kind} onChange={(e) => setKind(e.target.value)}>{Object.entries(FACT_KINDS).map(([k, v]) => <option key={k} value={k}>{v.title}</option>)}</select></div>}
        <div className="field"><label htmlFor="f-label">Short description</label><input id="f-label" name="label" required maxLength={300} defaultValue={fact?.label} /></div>
        {spec?.fields.map((f) => {
          const id = `f-${f.key}`, name = `v_${f.key}`, val = fact?.value?.[f.key];
          if (f.type === "check") return <label key={f.key} className="check-field"><input type="checkbox" name={name} defaultChecked={!!val} /> {f.label}</label>;
          if (f.type === "select") return <div className="field" key={f.key}><label htmlFor={id}>{f.label}</label><select id={id} name={name} defaultValue={val}>{f.options!.map(([k, l]) => <option key={k} value={k}>{l}</option>)}</select></div>;
          return <div className="field" key={f.key}><label htmlFor={id}>{f.label}</label><input id={id} name={name} type={f.type === "number" ? "number" : f.type === "date" ? "date" : "text"} min={f.type === "number" ? 0 : undefined} step="any" defaultValue={Array.isArray(val) ? val.join(", ") : val} /></div>;
        })}
        {spec?.dated && <div className="field"><label htmlFor="f-until">Expiry date</label><input id="f-until" name="valid_until" type="date" defaultValue={fact?.valid_until ?? undefined} /></div>}
        {fact?.evidence_status !== "document_on_file" && <div className="field"><label htmlFor="f-status">Do you hold this?</label>
          <select id="f-status" name="status" defaultValue={fact?.evidence_status === "needs_update" ? "declared" : fact?.evidence_status ?? "declared"}>
            <option value="declared">Yes, I declare it</option><option value="missing">No, we do not have it</option><option value="unknown">Not sure yet</option></select></div>}
        <footer className="dialog-foot"><button className="btn primary" disabled={busy}>{busy ? "Saving…" : "Save"}</button></footer>
      </form>
    </dialog>
  );
}

function SupplierCard({ onSaved }: { onSaved: () => Promise<void> }) {
  const { profile } = useProfile();
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  if (!editing) return <Card title="Supplier details" note={<button className="btn ghost sm" onClick={() => { setEditing(true); setError(null); }}>Edit</button>}>
    <p className="prose">{profile?.description || "No description yet."}</p>
    <div className="small muted">Delivery regions: {profile?.regions.join(", ") || "none entered"}</div>
  </Card>;
  return <Card title="Edit supplier details">
    <ErrorNote error={error} />
    <form onSubmit={async (e) => {
      e.preventDefault(); setBusy(true); setError(null);
      try { await api("/profile", { method: "PUT", body: JSON.stringify(profileBody(new FormData(e.currentTarget))) }); await onSaved(); setEditing(false); }
      catch (err: any) { setError(err.message); } finally { setBusy(false); }
    }}>
      <ProfileFields initial={profile ?? undefined} />
      <div className="actions"><button className="btn primary" disabled={busy}>{busy ? "Saving…" : "Save"}</button><button type="button" className="btn" onClick={() => setEditing(false)}>Cancel</button></div>
    </form>
  </Card>;
}

export default function Profile() {
  const { profile, refreshMe } = useProfile();
  usePageTitle(`Business profile · ${profile?.name ?? ""}`);
  const [facts, setFacts] = useState<any[] | null>(null);
  const [impact, setImpact] = useState<any[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const [dialog, setDialog] = useState<{ fact: any; renew: boolean } | null>(null);
  const [factDialog, setFactDialog] = useState<{ fact: any | null } | null>(null);

  const load = async () => {
    try {
      const [f, i] = await Promise.all([api<any[]>("/profile/facts"), api<any[]>("/profile/evidence-impact")]);
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
    if (!(d.get("expires_on") as string)) d.delete("expires_on");
    if (!(d.get("issuer") as string)) d.delete("issuer");
    if (!renew) { d.append("kind", fact.attribute); d.append("fact_id", String(fact.id)); }
    setBusy(true); setFormError(null);
    try {
      const out = await api(renew ? `/profile/facts/${fact.id}/renew` : "/profile/evidence", { method: "POST", body: d });
      const ok = renew ? out.verified : out.support_verified;
      if (!ok) { setFormError(renew ? "The file did not back this up: the sentence was not found on that page, or the file contradicts the fact." : "That sentence is not on that page, or the file contradicts the fact, so the fact was not marked as proven."); await load(); return; }
      setDialog(null); setToast(out.jobs?.length ? "Saved. Re-checking affected tenders…" : "Evidence checked and added.");
      await load();
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
        <SupplierCard onSaved={refreshMe} />
        <div className="section-actions"><button className="btn primary" onClick={() => setFactDialog({ fact: null })}>Add a fact</button></div>
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
                {dated && !expired(f) && <button className="btn sm" disabled={busy} onClick={() => act("Fact re-confirmed.", () => post("/confirmations", { point: "G4", target_id: f.id, decision: "reconfirmed" }))}>Re-confirm</button>}
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
                        onClick={() => act("Recorded: you checked this document yourself.", () => post(`/profile/facts/${f.id}/verify`, {}))}>I have checked this document</button>}
                    </div>
                    <div className="fact-side"><EvidencePill status={f.evidence_status}>{note(f)}</EvidencePill>
                      <div className="actions"><button className="btn ghost sm" disabled={busy} onClick={() => setFactDialog({ fact: f })} aria-label={`Edit ${f.label}`}>Edit</button>
                        <button className="btn ghost sm" disabled={busy} onClick={() => open(f, false)} aria-label={`Add evidence for ${f.label}`}>Evidence</button></div></div>
                  </div>
                ))}
              </Card>
            );
          })}
        </div>
        <p className="small muted">Declared capacity is not proof of availability for a given order, so each assessment asks you to confirm current workload.</p>
      </>}
      {factDialog && <FactDialog fact={factDialog.fact} onClose={() => setFactDialog(null)} onSaved={(message, jobs) => {
        setFactDialog(null); setToast(jobs.length ? "Saved. Re-checking affected tenders…" : message);
        load().then(() => (jobs.length ? waitForJobs(jobs).then(load) : undefined)).then(() => setToast(message)).catch((e) => setError(e.message));
      }} />}
      {dialog && <EvidenceDialog fact={dialog.fact} renew={dialog.renew} busy={busy} error={formError} onClose={() => setDialog(null)} onSubmit={submit} />}
      <Toast message={toast} busy={busy} onDone={() => setToast(null)} />
    </>
  );
}
