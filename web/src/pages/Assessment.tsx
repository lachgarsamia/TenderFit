import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { usePageTitle, useProfile } from "../App";
import { api, daysLeft, fmtDate, post, waitForJobs } from "../api";
import { Card, Empty, ErrorNote, EvidencePill, Fictional, Icon, OppStatus, Skeleton, Tabs, Tag, Tile, Toast, VerdictPill, human } from "../components/ui";

const LEVEL: Record<string, [string, string]> = { strong: ["Strong", "info"], partial: ["Partial", "warn"], low: ["Low", "bad"] };
const FEAS: Record<string, [string, string]> = { feasible: ["Feasible", "ok"], confirm: ["Confirm workload", "warn"], blocker: ["Blocker", "bad"], unknown: ["Not settled", ""] };
const ELIG_TONE: Record<string, string> = { blocker_found: "bad", provisional: "warn", not_assessed: "warn", needs_confirmation: "ok", no_blocker: "ok" };

const sourceOf = (r: any) => `${r.document}${r.section ? ` · §${r.section.split(" ")[0]}` : ""}${r.page ? ` · p.${r.page}` : ""}`;
const status = (r: any) => r.override_status ?? r.status;

function hint(r: any): string {
  if (r.override_status) return "overridden by you";
  if (r.needs_human === "G3") return "confirm workload";
  if (r.needs_human === "G2") return "proof needed";
  if (r.obligation === "scored") return "affects score only";
  if (r.status === "unknown") return /not connected|unavailable|budget/i.test(r.reason) ? "not checked yet" : "not proven either way";
  if (r.basis === "declared") return "on declared facts";
  if (r.basis === "documents") return "on documents";
  return "";
}

function howChecked(r: any): string[] {
  if (r.agent_steps?.length) {
    return r.agent_steps.map((s: any) => {
      const arg = s.input.query ?? s.input.label ?? s.input.attribute ?? "";
      return ({ search_passages: `Searched the documents for "${arg}"`, read_section: `Read ${arg || "the surrounding section"}`,
        open_attachment: `Opened ${arg}`, check_clarifications: `Checked clarifications for "${arg}"`,
        read_profile: `Checked your ${arg || "profile"} facts`, compare: "Ran a rule check against your profile",
        record_verdict: "Recorded the result with citations", raise_question: "Stopped and asked you a question" } as Record<string, string>)[s.tool] ?? s.tool;
    });
  }
  const first = ({ rule: "Checked with a fixed rule against your profile", model: "Read the cited passages and your facts",
    guard: "Read the passages; the result was checked against strict rules", budget: "Stopped at the cost limit",
    agent: "Investigated the documents step by step" } as Record<string, string>)[r.decided_by] ?? "Checked";
  return [first, human(r.reason)];
}

export default function Assessment() {
  const { lotId } = useParams();
  const { profile, refreshChanges } = useProfile();
  const [a, setA] = useState<any>(null);
  const [inv, setInv] = useState<any>(null);
  const [missing, setMissing] = useState(false);
  const [sel, setSel] = useState<number | null>(null);
  const [tab, setTab] = useState<"reqs" | "docs">("reqs");
  const [showMet, setShowMet] = useState(false);
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [answer, setAnswer] = useState("");
  const [reason, setReason] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);
  const [uploadRef, setUploadRef] = useState<number | null>(null);
  usePageTitle(a ? `Lot ${a.lot.lot_no} · ${a.lot.opportunity}` : "Assessment");

  const load = async () => {
    api(`/lots/${lotId}/documents`).then(setInv).catch(() => {});
    try {
      const d = await api(`/lots/${lotId}/assessment?profile_id=${profile!.id}`);
      setA(d); setMissing(false);
      setSel((s) => (s && d.requirements.some((r: any) => r.id === s) ? s :
        d.requirements.find((r: any) => status(r) !== "met")?.id ?? d.requirements[0]?.id ?? null));
    } catch (e: any) {
      if (e.message.includes("no assessment")) { setMissing(true); setA(null); } else setError(e.message);
    }
  };
  useEffect(() => { setSel(null); setTab("reqs"); setError(null); load(); }, [lotId, profile?.id]);

  const act = async (done: string, fn: () => Promise<any>) => {
    setBusy(true); setError(null); setToast("Saving…");
    try {
      const out = await fn();
      if (out?.jobs?.length) { setToast("Saved. Re-checking this lot…"); await waitForJobs(out.jobs); }
      await load(); refreshChanges(); setToast(done); setAnswer(""); setReason("");
    } catch (e: any) { setToast(null); setError(e.message); } finally { setBusy(false); }
  };
  const confirm = (point: string, target_id: number, decision: string, done: string, extra: any = {}) =>
    act(done, () => post("/confirmations", { point, target_id, decision, user: "you", profile_id: profile!.id, ...extra }));
  const upload = (refId: number | null) => { setUploadRef(refId); fileRef.current?.click(); };
  const onFile = (f?: File) => {
    if (!f) return;
    const fd = new FormData(); fd.append("file", f); fd.append("user", "you"); if (uploadRef) fd.append("ref_id", String(uploadRef));
    act("Document added and the lot re-assessed.", () => api(`/lots/${lotId}/documents`, { method: "POST", body: fd }));
  };

  const docs = inv?.documents ?? [];
  const essential = docs.filter((d: any) => d.essential && d.kind !== "notice");
  const unread = essential.filter((d: any) => d.status !== "read" && d.status !== "confirmed_none");
  const reqs = a?.requirements ?? [];
  const attention = reqs.filter((r: any) => status(r) !== "met");
  const met = reqs.filter((r: any) => status(r) === "met");
  const r = reqs.find((x: any) => x.id === sel);
  const unchecked = reqs.filter((x: any) => hint(x) === "not checked yet").length;
  const lot = a?.lot;
  const days = daysLeft(lot?.deadline);

  return (
    <>
      <input type="file" ref={fileRef} hidden accept=".pdf,.docx" onChange={(e) => { onFile(e.target.files?.[0]); e.target.value = ""; }} />
      <div className="crumb"><Link to="/opportunities">Opportunities</Link> / {lot?.buyer ?? "…"}</div>
      <div className="page-head">
        <div style={{ minWidth: 0 }}>
          <div className="title-row">
            <h1>{lot ? `Lot ${lot.lot_no}${lot.title ? ` · ${lot.title}` : ""}` : "Lot"}</h1>
            {a?.fictional_notice && <Fictional>Demo notice · scripted AI answers</Fictional>}
          </div>
          {a && <div className="meta">
            <span>{lot.opportunity}</span>
            <span>Version {a.version?.version_no} of {a.version?.versions}</span>
            <span>Deadline {fmtDate(lot.deadline)}{days !== null && days >= 0 ? ` (${days} days)` : ""}</span>
            <OppStatus status={a.status.status} suffix={`checked ${fmtDate(a.status.last_checked_at)}`} />
          </div>}
        </div>
        {a && <div className="actions">
          {a.version?.source_url && !a.fictional_notice && <a className="btn ghost" href={a.version.source_url} target="_blank" rel="noreferrer"><Icon name="external" size={16} />Source notice</a>}
          {a.decision?.decision === "pursue" ? <>
            <button className="btn" disabled={busy} onClick={() => confirm("G1", Number(lotId), "not_for_us", "Marked as not for you.")}>Not for us</button>
            <Link className="btn primary" to={`/lots/${lotId}/preparation`}>Preparation plan</Link>
          </> : <>
            <button className="btn" disabled={busy || a.decision?.decision === "not_for_us"} onClick={() => confirm("G1", Number(lotId), "not_for_us", "Marked as not for you.")}>
              {a.decision?.decision === "not_for_us" ? "Marked not for us" : "Not for us"}</button>
            <button className="btn primary" disabled={busy} onClick={() => confirm("G1", Number(lotId), "pursue", "You are pursuing this lot. Your preparation plan is ready.")}>Pursue this lot</button>
          </>}
        </div>}
      </div>

      <ErrorNote error={error} />
      {a && !a.on_current_version && <div className="banner info"><span className="b-icon">i</span><div className="body"><strong>The buyer published a newer version.</strong>
        <p>This assessment is being re-checked against it. See <Link to="/changes">Changes</Link>.</p></div></div>}
      {unread.length > 0 && (
        <div className="banner warn">
          <span className="b-icon">!</span>
          <div className="body">
            <strong>{a ? "Provisional: " : ""}{essential.length - unread.length} of {essential.length} essential documents read</strong>
            <p>{unread.map((d: any) => d.label).join(", ")} could not be read, so no blocker is claimed for {unread.length === 1 ? "it" : "them"}. Upload it from the buyer's portal, or confirm the buyer issued none.</p>
          </div>
          <button className="btn" onClick={() => setTab("docs")}>View documents</button>
          <button className="btn primary" disabled={busy} onClick={() => upload(unread[0]?.id)}>Upload</button>
        </div>
      )}

      {missing && (
        <Card title="Not assessed yet">
          <p className="muted" style={{ marginTop: 0, maxWidth: "70ch" }}>TenderFit will read the documents it can reach, list every requirement with its source quote, and check each one against {profile?.name}'s facts. It never guesses: anything it cannot prove is marked Unknown.</p>
          <button className="btn primary" disabled={busy} onClick={() => act("Assessment ready.", () => post(`/lots/${lotId}/assess`, { profile_id: profile!.id }))}>Assess this lot</button>
        </Card>
      )}
      {!a && !missing && !error && <Card><Skeleton lines={5} /></Card>}

      {unchecked > 0 && <div className="banner info"><span className="b-icon">i</span><div className="body">
        <strong>{unchecked} requirement{unchecked > 1 ? "s were" : " was"} not checked yet</strong>
        <p>The AI service that reads open-ended requirements is not connected, so these are marked Unknown instead of guessed. Rule-based checks are unaffected.</p></div></div>}
      {a && <Card>
        <div className="verdict-line"><strong>{a.recommendation}</strong></div>
        <div className="tiles">
          <Tile label="Relevance" value={LEVEL[a.dimensions.relevance.level]?.[0]} tone={LEVEL[a.dimensions.relevance.level]?.[1]} detail={a.dimensions.relevance.detail} />
          <Tile label="Eligibility" value={a.dimensions.eligibility.label} tone={ELIG_TONE[a.dimensions.eligibility.state]} detail={a.dimensions.eligibility.detail.split("; unread")[0]} />
          <Tile label="Evidence" value={a.dimensions.evidence.gaps ? `${a.dimensions.evidence.gaps} gap${a.dimensions.evidence.gaps > 1 ? "s" : ""}` : "No gaps"}
                tone={a.dimensions.evidence.gaps ? "warn" : "ok"} detail={a.dimensions.evidence.gaps ? a.dimensions.evidence.detail : "Every check has support"} />
          <Tile label="Feasibility" value={FEAS[a.dimensions.feasibility.level]?.[0]} tone={FEAS[a.dimensions.feasibility.level]?.[1]} detail={human(a.dimensions.feasibility.detail)} />
          <Tile label="Open questions" value={a.dimensions.open_questions.count ? `${a.dimensions.open_questions.count} for you` : "None"} tone="info"
                detail={a.dimensions.open_questions.items[0]} />
        </div>
      </Card>}

      {(a || inv) && <Card flush>
        <Tabs label="Assessment sections" value={a ? tab : "docs"} onChange={setTab} tabs={[
          ...(a ? [{ id: "reqs" as const, label: "Requirements", count: reqs.length }] : []),
          { id: "docs" as const, label: `Documents${inv ? ` · ${essential.length - unread.length} of ${essential.length} essential read` : ""}` },
        ]} />

        {a && tab === "reqs" && (reqs.length === 0 ? <Empty title="No requirements found">Nothing in the documents read was recognised as a requirement. Check the documents yourself.</Empty> : (
          <div className="split" style={{ padding: "0 0 4px" }}>
            <div>
              {attention.length > 0 && <>
                <div className="group-head"><span>Needs your attention · {attention.length}</span></div>
                {attention.map((x: any) => <ReqRow key={x.id} x={x} current={x.id === sel} onClick={() => setSel(x.id)} />)}
              </>}
              {met.length > 0 && <>
                <div className="group-head"><span>Met · {met.length}</span>
                  <button className="btn ghost sm" onClick={() => setShowMet((v) => !v)} aria-expanded={showMet}>{showMet ? "Hide" : "Show"}</button></div>
                {showMet && met.map((x: any) => <ReqRow key={x.id} x={x} current={x.id === sel} onClick={() => setSel(x.id)} />)}
              </>}
            </div>
            <div className="sticky" style={{ padding: "14px 20px 18px", borderLeft: "1px solid var(--line-soft)" }}>
              {!r ? <Empty>Select a requirement to see its evidence.</Empty> : <Evidence r={r} busy={busy} answer={answer} setAnswer={setAnswer} reason={reason} setReason={setReason}
                onAnswer={() => act("Answer saved and the requirement re-checked.", () => post(`/questions/${r.question_id}/answer`, { answer, user: "you" }))}
                onConfirm={(point, decision, done, extra) => confirm(point, r.id, decision, done, extra)} />}
            </div>
          </div>
        ))}

        {(tab === "docs" || !a) && (!inv ? <div style={{ padding: 20 }}><Skeleton /></div> : (
          <table>
            <thead><tr><th>Document</th><th>Where it comes from</th><th>Status</th><th><span className="sr-only">Action</span></th></tr></thead>
            <tbody>
              {docs.map((d: any) => {
                const blocking = d.essential && d.status !== "read" && d.status !== "confirmed_none" && d.kind !== "notice";
                return (
                  <tr key={d.id} className={blocking ? "attention" : ""}>
                    <td><div style={{ fontWeight: 500 }}>{d.label === d.ocds_type && d.filename ? d.filename : d.label}</div>
                      <div className="mono">{d.essential ? "essential" : "supporting"}{d.filename && d.filename !== d.label ? ` · ${d.filename}` : ""}</div></td>
                    <td className="small">{d.kind === "notice" ? "Find a Tender notice" : d.kind === "referenced" ? `Mentioned in ${d.referenced_in}` :
                      d.origin === "upload" ? "Uploaded by you" : d.status === "read" ? "Public download" : d.url ? "Buyer portal (registration needed)" : "Listed without a link"}</td>
                    <td>{d.status === "read" ? <Tag tone="ok">✓ Read · {d.passages} passages</Tag> :
                      d.status === "confirmed_none" ? <Tag tone="muted">Buyer issued none</Tag> :
                      d.status === "unreadable" ? <Tag tone="bad">✕ Unreadable</Tag> :
                      blocking ? <Tag tone="bad">✕ Not read · blocks a final verdict</Tag> : <Tag tone="muted">Not needed for the check</Tag>}</td>
                    <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>{blocking && <>
                      <button className="btn ghost sm" disabled={busy} onClick={() => confirm("G7", d.id, "none_issued", "Recorded: the buyer issued none.")}>Buyer issued none</button>
                      <button className="btn sm" disabled={busy} onClick={() => upload(d.id)}>Upload</button></>}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        ))}
      </Card>}
      <Toast message={toast} busy={busy} onDone={() => setToast(null)} />
    </>
  );
}

function ReqRow({ x, current, onClick }: { x: any; current: boolean; onClick: () => void }) {
  return (
    <button className="req" aria-current={current} onClick={onClick}>
      <div style={{ minWidth: 0 }}>
        <div className="req-title">{x.statement}</div>
        <div className="src">{x.obligation === "mandatory" ? "Mandatory" : x.obligation === "scored" ? "Scored" : "Information"} · {sourceOf(x)}</div>
      </div>
      <div style={{ textAlign: "right" }}><VerdictPill status={status(x)} /><div className="small muted" style={{ marginTop: 3 }}>{hint(x)}</div></div>
    </button>
  );
}

function Evidence({ r, busy, answer, setAnswer, reason, setReason, onAnswer, onConfirm }: {
  r: any; busy: boolean; answer: string; setAnswer: (s: string) => void; reason: string; setReason: (s: string) => void;
  onAnswer: () => void; onConfirm: (point: string, decision: string, done: string, extra?: any) => void;
}) {
  const extra = r.citations.filter((c: any) => c.quote !== r.quote);
  return (
    <>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 10, alignItems: "flex-start" }}>
        <h2 style={{ fontSize: 15 }}>{r.statement}</h2><VerdictPill status={status(r)} />
      </div>
      <div className="label">What the tender says</div>
      <div className="quote"><div className="src">{r.document}{r.section ? ` · §${r.section}` : ""}{r.page ? ` · p.${r.page}` : ""}</div>"{r.quote}"</div>
      {extra.map((c: any, i: number) => <div className="quote" style={{ marginTop: 6 }} key={i}><div className="src">Also cited{c.section ? ` · ${c.section}` : ""}{c.page ? ` · p.${c.page}` : ""}</div>"{c.quote}"</div>)}

      {hint(r) === "not checked yet" && <div className="notice">{human(r.reason)}</div>}
      <div className="label">What we know about you</div>
      {r.facts.length ? r.facts.map((f: any) => <div className="fact-line" key={f.id}><span>{f.label}</span><EvidencePill status={f.evidence_status} /></div>)
        : <div className="small muted">Nothing in your profile covers this yet.</div>}

      {r.needs_human === "G3" && <div className="ask-box"><div className="label">Your next step</div>
        <div className="small">Your declared lead time fits. Confirm you have capacity for this order now.</div>
        <button className="btn primary sm" style={{ marginTop: 10 }} disabled={busy} onClick={() => onConfirm("G3", "confirm_for_lot", "Workload confirmed for this lot.")}>Confirm current workload</button></div>}
      {r.needs_human === "G2" && <div className="ask-box"><div className="label">Your next step</div>
        <div className="small">The buyer asks for proof, and only a declaration is on file. Upload the evidence, or confirm the declaration for this lot.</div>
        <div className="actions" style={{ marginTop: 10 }}>
          <Link className="btn primary sm" to="/profile">Add evidence</Link>
          <button className="btn sm" disabled={busy} onClick={() => onConfirm("G2", "confirm_declaration", "Declaration confirmed for this lot.")}>Confirm declaration</button></div></div>}
      {status(r) === "not_met" && <div className="ask-box"><div className="label">This is a blocker</div>
        <div className="small">If you know it is not, you can override it with a written reason. Your reason is kept with the assessment.</div>
        <textarea aria-label="Reason for override" value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Why this is not a blocker" />
        <button className="btn sm" disabled={!reason.trim() || busy} onClick={() => onConfirm("G6", "override", "Override recorded with your reason.", { reason })}>Override with reason</button></div>}
      {r.question && !["G2", "G3"].includes(r.needs_human) && status(r) !== "not_met" && <div className="ask-box"><div className="label">Question for you</div>
        <div>{r.question}</div>
        <textarea aria-label="Your answer" value={answer} onChange={(e) => setAnswer(e.target.value)} placeholder="Your answer is recorded as a declaration, not proof" />
        <div className="actions"><button className="btn primary sm" disabled={!answer.trim() || busy || !r.question_id} onClick={onAnswer}>Save answer</button>
          <Link className="btn sm" to="/profile">Add evidence instead</Link></div></div>}
      {r.override_status && <div className="small" style={{ marginTop: 12 }}>Overridden by {r.override_by}: "{r.override_reason}"</div>}

      <details className="more">
        <summary>How this was decided</summary>
        <div><ol className="checks">{howChecked(r).map((s, i) => <li key={i}>{s}</li>)}</ol></div>
        {r.escalate_reason && <div>Sent for a closer look because: {human(r.escalate_reason).replace(/;/g, ",")}.</div>}
      </details>
    </>
  );
}
