import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { usePageTitle, useProfile } from "../App";
import { api, daysLeft, fmtDate, post } from "../api";
import { ProgressBanner } from "../components/ProgressBanner";
import { SourceDialog, type SourceRef } from "../components/SourceDialog";
import { Card, DeciderTag, Empty, ErrorNote, EvidencePill, Fictional, Icon, OppStatus, QualityTag, Skeleton, Tabs, Tag, Tile, Toast, VerdictPill, deciderExplain, human } from "../components/ui";

const LEVEL: Record<string, [string, string]> = { strong: ["Strong", "info"], partial: ["Partial", "warn"], low: ["Low", "bad"] };
const FEAS: Record<string, [string, string]> = { feasible: ["Feasible", "ok"], confirm: ["Confirm workload", "warn"], blocker: ["Blocker", "bad"], unknown: ["Not settled", ""] };
const ELIG_TONE: Record<string, string> = { blocker_found: "bad", provisional: "warn", not_assessed: "warn", needs_confirmation: "ok", no_blocker: "ok" };
const CHECKED: Record<string, string> = { exists: "File on record; contents not confirmed", relevant: "File names the subject; the claim is not confirmed", supports: "File supports the claim, but has expired", current: "File supports the claim and is current" };
const POLL_MS = 2000;

const sourceOf = (r: any) => `${r.document}${r.section ? ` · §${r.section.split(" ")[0]}` : ""}${r.page ? ` · p.${r.page}` : ""}`;
const status = (r: any) => r.override_status ?? r.status;

function hint(r: any): string {
  if (r.override_status) return "overridden by you";
  if (r.needs_human === "G5") return "ask the buyer";
  if (r.needs_human === "G3") return "confirm workload";
  if (r.needs_human === "G2") return "proof needed";
  if (r.needs_human === "G4") return "update needed";
  if (r.obligation === "scored") return "affects score only";
  if (r.status === "unknown") return /not connected|unavailable|budget/i.test(r.reason) ? "not checked yet" : "not proven either way";
  if (r.basis === "declared") return "on declared facts";
  if (r.basis === "documents") return "on documents";
  return "";
}

function nextStep(r: any): string | null {
  const s = status(r);
  if (s === "met") return r.evidence_quality === "weak" ? "Add a document if you can. The buyer may ask for proof of what you declared." : null;
  if (r.needs_human === "G5") return "Raise this with the buyer through their clarification process, then record what they say.";
  if (r.needs_human === "G4") return "Update this fact in your profile, or add the renewed document.";
  if (s === "not_met") return "If this is wrong, correct the fact in your profile. Otherwise this lot may not be open to you.";
  if (s === "unknown") return r.obligation === "scored" ? "This affects your score, not your eligibility. Prepare it for the response." : "Add the fact or the evidence, or answer the question below.";
  return "Answer the question below, or add the document that settles it.";
}

function howChecked(r: any): string[] {
  if (!r.agent_steps?.length) return [];
  return r.agent_steps.map((s: any) => {
    const arg = s.input.query ?? s.input.label ?? s.input.attribute ?? "";
    return ({ search_passages: `Searched the documents for "${arg}"`, read_section: `Read ${arg || "the surrounding section"}`,
      open_attachment: `Opened ${arg}`, check_clarifications: `Checked clarifications for "${arg}"`, check_evidence: "Checked the file behind a fact",
      read_profile: `Checked your ${arg || "profile"} facts`, compare: "Ran a rule check against your profile",
      record_verdict: "Recorded the result with citations", raise_question: "Stopped and asked you a question" } as Record<string, string>)[s.tool] ?? s.tool;
  });
}

export default function Assessment() {
  const { lotId } = useParams();
  const { profile, refreshChanges } = useProfile();
  const [lot, setLot] = useState<any>(null);
  const [a, setA] = useState<any>(null);
  const [inv, setInv] = useState<any>(null);
  const [loaded, setLoaded] = useState(false);
  const [sel, setSel] = useState<number | null>(null);
  const [tab, setTab] = useState<"reqs" | "docs">("reqs");
  const [showMet, setShowMet] = useState(false);
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [answer, setAnswer] = useState("");
  const [reason, setReason] = useState("");
  const [source, setSource] = useState<SourceRef | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const [uploadRef, setUploadRef] = useState<number | null>(null);
  usePageTitle(lot ? `Lot ${lot.lot_no} · ${lot.opportunity}` : "Assessment");

  const load = useCallback(async () => {
    try {
      setInv(await api(`/lots/${lotId}/documents`));
    } catch (e: any) { setError(e.message); }
    try {
      const d = await api(`/lots/${lotId}/assessment`);
      setA(d); setError(null);
      setSel((s) => (s && d.requirements.some((r: any) => r.id === s) ? s :
        d.requirements.find((r: any) => status(r) !== "met")?.id ?? d.requirements[0]?.id ?? null));
    } catch (e: any) {
      if (e.message.includes("no assessment")) setA(null); else setError(e.message);
    } finally { setLoaded(true); }
  }, [lotId, profile?.id]);

  useEffect(() => {
    setSel(null); setTab("reqs"); setError(null); setLoaded(false); setA(null); setInv(null); setLot(null);
    api(`/lots/${lotId}`).then(setLot).catch((e) => setError(e.message));
    load();
  }, [lotId, profile?.id]);

  // The server knows whether work is under way, so progress survives a reload or a second tab.
  const processing = a?.processing;
  const wasProcessing = useRef(false);
  useEffect(() => {
    if (!processing) {
      if (wasProcessing.current) { wasProcessing.current = false; refreshChanges(); }
      return;
    }
    wasProcessing.current = true;
    const t = setTimeout(load, POLL_MS);
    return () => clearTimeout(t);
  }, [processing, load]);

  const act = async (done: string, fn: () => Promise<any>) => {
    setBusy(true); setError(null);
    try {
      await fn();
      await load(); refreshChanges(); setToast(done); setAnswer(""); setReason("");
    } catch (e: any) { setError(e.message); } finally { setBusy(false); }
  };
  const confirm = (point: string, target_id: number, decision: string, done: string, extra: any = {}) =>
    act(done, () => post("/confirmations", { point, target_id, decision, ...extra }));
  const upload = (refId: number | null) => { setUploadRef(refId); fileRef.current?.click(); };
  const onFile = (f?: File) => {
    if (!f) return;
    const fd = new FormData(); fd.append("file", f);
    if (uploadRef) fd.append("ref_id", String(uploadRef));
    act("Document added, privately to your workspace.", () => api(`/lots/${lotId}/documents`, { method: "POST", body: fd }));
  };
  const assess = () => act("Assessment started.", () => post(`/lots/${lotId}/assess`, {}));

  const hasResult = !!a?.assessment_id;
  const docs = inv?.documents ?? [];
  const essential = docs.filter((d: any) => d.essential && d.kind !== "notice");
  const unread = essential.filter((d: any) => d.status !== "read" && d.status !== "confirmed_none");
  const reqs = a?.requirements ?? [];
  const attention = reqs.filter((r: any) => status(r) !== "met");
  const met = reqs.filter((r: any) => status(r) === "met");
  const r = reqs.find((x: any) => x.id === sel);
  const unchecked = reqs.filter((x: any) => hint(x) === "not checked yet").length;
  const days = daysLeft(lot?.deadline);
  const siblings = lot?.lots ?? [];
  const rejectedMandatory = (a?.rejections ?? []).filter((x: any) => x.kind !== "condition" && x.obligation === "mandatory");
  const dims = hasResult ? a.dimensions : null;

  return (
    <>
      <input type="file" ref={fileRef} hidden accept=".pdf,.docx" onChange={(e) => { onFile(e.target.files?.[0]); e.target.value = ""; }} />
      <div className="crumb"><Link to="/opportunities">Opportunities</Link>{lot && <> / <Link to={`/tenders/${lot.opportunity_id}`}>{lot.opportunity}</Link></>}</div>
      <div className="page-head">
        <div style={{ minWidth: 0 }}>
          <div className="title-row">
            <h1>{lot ? `Lot ${lot.lot_no}${lot.title ? ` · ${lot.title}` : ""}` : "Lot"}</h1>
            {a?.fictional_notice && <Fictional>Demo notice · scripted AI answers</Fictional>}
          </div>
          {lot && <div className="meta">
            <span>{lot.buyer}</span>
            {a?.version && <span>Version {a.version.version_no} of {a.version.versions}</span>}
            <span>Deadline {fmtDate(lot.deadline)}{days !== null && days >= 0 ? ` (${days} days)` : ""}</span>
            {a?.status && <OppStatus status={a.status.status} suffix={`checked ${fmtDate(a.status.last_checked_at)}`} />}
          </div>}
        </div>
        {hasResult && <div className="actions">
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

      {siblings.length > 1 && <nav className="lot-switch" aria-label="Lots in this tender">
        {siblings.map((l: any) => <Link key={l.id} to={`/lots/${l.id}`} aria-current={String(l.id) === lotId ? "page" : undefined}>Lot {l.lot_no}{l.title ? ` · ${l.title}` : ""}</Link>)}
      </nav>}

      <ErrorNote error={error} />
      {processing && <ProgressBanner p={processing} hasResult={hasResult} />}
      {a?.last_failure && !processing && <div className="banner warn" role="alert"><span className="b-icon">!</span><div className="body">
        <strong>The last update did not finish</strong>
        <p>{human(a.last_failure.error)} {hasResult ? "The result below is the last completed one." : "No result has been produced yet."}</p>
      </div><button className="btn primary" disabled={busy} onClick={assess}>Try again</button></div>}
      {hasResult && !a.on_current_version && !processing && <div className="banner info"><span className="b-icon">i</span><div className="body"><strong>The buyer published a newer version.</strong>
        <p>This result is for the earlier version. See <Link to="/changes">Changes</Link> for the state of the re-check.</p></div></div>}
      {unread.length > 0 && (
        <div className="banner warn">
          <span className="b-icon">!</span>
          <div className="body">
            <strong>{hasResult ? "Provisional: " : ""}{essential.length - unread.length} of {essential.length} essential documents read</strong>
            <p>{unread.map((d: any) => d.label).join(", ")} could not be read in full, so no blocker is claimed for {unread.length === 1 ? "it" : "them"}. Upload {unread.length === 1 ? "it" : "them"} from the buyer's portal (private to you), or confirm the buyer issued none.</p>
            {unread.filter((d: any) => d.ocr_pages?.length).map((d: any) => <p key={d.id}><strong>{d.label}:</strong> {d.note}</p>)}
          </div>
          <button className="btn" onClick={() => setTab("docs")}>View documents</button>
          <button className="btn primary" disabled={busy} onClick={() => upload(unread[0]?.id)}>Upload</button>
        </div>
      )}
      {rejectedMandatory.length > 0 && <div className="banner warn"><span className="b-icon">!</span><div className="body">
        <strong>{rejectedMandatory.length} possible mandatory requirement{rejectedMandatory.length > 1 ? "s were" : " was"} not accepted</strong>
        <p>The extraction proposed {rejectedMandatory.length > 1 ? "them" : "it"}, but the source text did not back {rejectedMandatory.length > 1 ? "them" : "it"} up. Check {rejectedMandatory.length > 1 ? "them" : "it"} under Documents.</p>
      </div></div>}

      {loaded && !hasResult && !processing && (
        <Card title="Not assessed yet">
          <p className="muted" style={{ marginTop: 0, maxWidth: "70ch" }}>TenderFit will read the documents it can reach, list every requirement with its source quote, and check each one against {profile?.name}'s facts. It never guesses: anything it cannot prove is marked Unknown.</p>
          <button className="btn primary" disabled={busy} onClick={assess}>Assess this lot</button>
        </Card>
      )}
      {!loaded && <Card><Skeleton lines={5} /></Card>}

      {unchecked > 0 && <div className="banner info"><span className="b-icon">i</span><div className="body">
        <strong>{unchecked} requirement{unchecked > 1 ? "s were" : " was"} not checked yet</strong>
        <p>The AI service that reads open-ended requirements was unavailable, so these are marked Unknown instead of guessed. Rule-based checks are unaffected.</p></div></div>}
      {dims && <Card>
        <div className="verdict-line"><strong>{a.recommendation}</strong></div>
        <div className="tiles">
          <Tile label="Relevance" value={LEVEL[dims.relevance.level]?.[0]} tone={LEVEL[dims.relevance.level]?.[1]} detail={dims.relevance.detail} />
          <Tile label="Eligibility" value={dims.eligibility.label} tone={ELIG_TONE[dims.eligibility.state]} detail={dims.eligibility.detail.split("; unread")[0]} />
          <Tile label="Evidence" value={dims.evidence.gaps ? `${dims.evidence.gaps} gap${dims.evidence.gaps > 1 ? "s" : ""}` : "No gaps"}
                tone={dims.evidence.gaps ? "warn" : "ok"} detail={dims.evidence.gaps ? dims.evidence.detail : "Every check has support"} />
          <Tile label="Feasibility" value={FEAS[dims.feasibility.level]?.[0]} tone={FEAS[dims.feasibility.level]?.[1]} detail={human(dims.feasibility.detail)} />
          <Tile label="Open questions" value={dims.open_questions.count ? `${dims.open_questions.count} for you` : "None"} tone="info" detail={dims.open_questions.items[0]} />
        </div>
      </Card>}

      {(hasResult || inv) && <Card flush>
        <Tabs label="Assessment sections" value={hasResult ? tab : "docs"} onChange={setTab} tabs={[
          ...(hasResult ? [{ id: "reqs" as const, label: "Requirements", count: reqs.length }] : []),
          { id: "docs" as const, label: `Documents${inv ? ` · ${essential.length - unread.length} of ${essential.length} essential read` : ""}` },
        ]} />

        {hasResult && tab === "reqs" && (reqs.length === 0 ? <Empty title="No requirements found">Nothing in the documents read was recognised as a requirement. Check the documents yourself.</Empty> : (
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
            <div className="sticky evidence-pane">
              {!r ? <Empty>Select a requirement to see its evidence.</Empty> : <Evidence r={r} busy={busy} answer={answer} setAnswer={setAnswer} reason={reason} setReason={setReason} onSource={setSource}
                onAnswer={() => act("Answer saved. Re-checking.", () => post(`/questions/${r.question_id}/answer`, { answer }))}
                onConfirm={(point, decision, done, extra) => confirm(point, r.id, decision, done, extra)} />}
            </div>
          </div>
        ))}

        {(tab === "docs" || !hasResult) && (!inv ? <div style={{ padding: 20 }}><Skeleton /></div> : <>
          <div className="table-wrap"><table>
            <thead><tr><th>Document</th><th>Where it comes from</th><th>Status</th><th><span className="sr-only">Action</span></th></tr></thead>
            <tbody>
              {docs.map((d: any) => {
                const blocking = d.essential && d.status !== "read" && d.status !== "confirmed_none" && d.kind !== "notice";
                return (
                  <tr key={d.id} className={blocking ? "attention" : ""}>
                    <td><div style={{ fontWeight: 500 }}>{d.label === d.ocds_type && d.filename ? d.filename : d.label}</div>
                      <div className="mono">{d.essential ? "essential" : "supporting"}{d.filename && d.filename !== d.label ? ` · ${d.filename}` : ""}</div></td>
                    <td className="small">{d.kind === "notice" ? "Find a Tender notice" : d.kind === "referenced" ? `Mentioned in ${d.referenced_in}` :
                      d.origin === "upload" ? "Uploaded by you (private)" : d.status === "read" ? "Public download" : d.url ? "Buyer portal (registration needed)" : "Listed without a link"}</td>
                    <td>{d.status === "read" ? <Tag tone="ok">✓ Read · {d.passages} passages</Tag> :
                      d.status === "confirmed_none" ? <Tag tone="muted">Buyer issued none</Tag> :
                      d.status === "partial" ? <><Tag tone="warn">Partly read · {d.passages} passages</Tag><div className="small muted" style={{ marginTop: 4 }}>{d.note}</div></> :
                      d.status === "unreadable" ? <><Tag tone="bad">✕ Unreadable</Tag>{d.note && <div className="small muted" style={{ marginTop: 4 }}>{d.note}</div>}</> :
                      blocking ? <Tag tone="bad">✕ Not read · blocks a final verdict</Tag> : <Tag tone="muted">Not needed for the check</Tag>}</td>
                    <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>{blocking && <>
                      <button className="btn ghost sm" disabled={busy} onClick={() => confirm("G7", d.id, "none_issued", "Recorded for your workspace: the buyer issued none.")}>Buyer issued none</button>
                      <button className="btn sm" disabled={busy} onClick={() => upload(d.id)}>Upload</button></>}</td>
                  </tr>
                );
              })}
            </tbody>
          </table></div>
          {(a?.rejections?.length ?? 0) > 0 && <details className="more" style={{ padding: "4px 20px 16px" }}>
            <summary>Not accepted from extraction · {a.rejections.length}</summary>
            <p className="small muted">The extraction proposed these, but a check failed. They are listed so nothing disappears unnoticed.</p>
            <ul className="plain-list compact">{a.rejections.map((x: any, i: number) => <li key={i}><div style={{ minWidth: 0 }}><div>{x.statement ?? "Unreadable response"}</div><div className="small muted">{x.reason}</div></div>
              {x.obligation === "mandatory" && <Tag tone="warn">Mandatory</Tag>}</li>)}</ul>
          </details>}
        </>)}
      </Card>}
      {source && <SourceDialog source={source} onClose={() => setSource(null)} />}
      <Toast message={toast} busy={false} onDone={() => setToast(null)} />
    </>
  );
}

function ReqRow({ x, current, onClick }: { x: any; current: boolean; onClick: () => void }) {
  return (
    <button className="req" aria-current={current} onClick={onClick}>
      <div style={{ minWidth: 0 }}>
        <div className="req-title">{x.statement}</div>
        <div className="src">{x.obligation === "mandatory" ? "Mandatory" : x.obligation === "scored" ? "Scored" : "Information"} · {sourceOf(x)}{x.carried_over ? " · unchanged by the last amendment" : ""}</div>
      </div>
      <div style={{ textAlign: "right" }}><VerdictPill status={status(x)} /><div className="small muted" style={{ marginTop: 3 }}>{hint(x)}</div></div>
    </button>
  );
}

function Evidence({ r, busy, answer, setAnswer, reason, setReason, onAnswer, onConfirm, onSource }: {
  r: any; busy: boolean; answer: string; setAnswer: (s: string) => void; reason: string; setReason: (s: string) => void;
  onAnswer: () => void; onConfirm: (point: string, decision: string, done: string, extra?: any) => void; onSource: (s: SourceRef) => void;
}) {
  const own = r.citations.find((c: any) => c.quote === r.quote);
  const extra = r.citations.filter((c: any) => c.quote !== r.quote);
  const consulted = (r.retrieval ?? []).filter((x: any) => x.why?.some((w: string) => /refers to|defined term|second search/.test(w)));
  const step = nextStep(r);
  const steps = howChecked(r);
  return (
    <>
      <div className="evidence-head">
        <h2>{r.statement}</h2>
        <div className="pills"><VerdictPill status={status(r)} /><DeciderTag by={r.decided_by} /><QualityTag q={r.evidence_quality} /></div>
      </div>

      <div className="label">What the tender requires</div>
      <button className="quote as-link" disabled={!own} onClick={() => own && onSource({ passage_id: own.passage_id, quote: own.quote, page: own.page })}>
        <div className="src">{r.document}{r.section ? ` · §${r.section}` : ""}{r.page ? ` · p.${r.page}` : ""} <span className="open-hint">Read in context ›</span></div>"{r.quote}"
      </button>
      {extra.map((c: any, i: number) => <button className="quote as-link" key={i} style={{ marginTop: 6 }} onClick={() => onSource({ passage_id: c.passage_id, quote: c.quote, page: c.page })}>
        <div className="src">Also cited · {c.document}{c.section ? ` · ${c.section}` : ""}{c.page ? ` · p.${c.page}` : ""} <span className="open-hint">Read in context ›</span></div>"{c.quote}"</button>)}

      {(r.required || r.observed) && <dl className="compare">
        {r.required && <><dt>Required</dt><dd>{r.required}</dd></>}
        {r.observed && <><dt>You have</dt><dd>{r.observed}</dd></>}
      </dl>}

      {r.conditions?.length > 1 && <>
        <div className="label">Conditions checked separately</div>
        <ul className="conditions">{r.conditions.map((c: any, i: number) => <li key={i}>
          <div style={{ minWidth: 0 }}><div>{c.label}{!c.mandatory && <span className="muted"> (optional)</span>}</div><div className="small muted">{human(c.reason)}</div></div>
          <VerdictPill status={c.status} />
        </li>)}</ul>
      </>}

      <div className="label">What we know about you</div>
      {r.facts.length ? r.facts.map((f: any) => <div className="fact-block" key={f.id}>
        <div className="fact-line"><span>{f.label}</span><EvidencePill status={f.evidence_status} /></div>
        {f.supports?.map((s: any, i: number) => <div className="small muted" key={i}>▤ {s.filename}{s.page ? ` · p.${s.page}` : ""}{s.human_verified_by ? ` · checked by ${s.human_verified_by}` : ""}</div>)}
        {f.verification_level && <div className="small muted">{CHECKED[f.verification_level]}</div>}
      </div>) : <div className="small muted">Nothing in your profile covers this yet.</div>}

      <div className="label">Why this result</div>
      <p className="why">{human(r.reason)}</p>
      <p className="small muted" style={{ marginTop: 4 }}>{deciderExplain(r.decided_by)}</p>
      {r.uncertainty && status(r) !== "not_met" && <><div className="label">What remains uncertain</div><p className="why">{r.uncertainty}</p></>}
      {r.condition_note && <div className="notice">A machine check was proposed for this requirement but not trusted: {r.condition_note}. It was read instead.</div>}
      {step && <div className="next"><span className="label">Next step</span>{step}</div>}

      {r.needs_human === "G3" && <div className="ask-box"><div className="label">Confirm for this order</div>
        <div className="small">Your declared lead time fits. Confirm you have capacity for this order now. This holds for this version of the notice.</div>
        <button className="btn primary sm" style={{ marginTop: 10 }} disabled={busy} onClick={() => onConfirm("G3", "confirm_for_lot", "Workload confirmed. Re-checking.")}>Confirm current workload</button></div>}
      {r.needs_human === "G2" && <div className="ask-box"><div className="label">Proof needed</div>
        <div className="small">The buyer asks for proof, and only a declaration is on file. Upload the evidence, or confirm the declaration for this lot.</div>
        <div className="actions" style={{ marginTop: 10 }}>
          <Link className="btn primary sm" to="/profile">Add evidence</Link>
          <button className="btn sm" disabled={busy} onClick={() => onConfirm("G2", "confirm_declaration", "Declaration confirmed. Re-checking.")}>Confirm declaration</button></div></div>}
      {r.status === "not_met" && !r.override_status && <div className="ask-box"><div className="label">If this is not really a blocker</div>
        <div className="small">You can override it with a written reason. Your reason stays with the assessment across amendments, unless the requirement's wording changes.</div>
        <textarea aria-label="Reason for override" value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Why this is not a blocker" />
        <button className="btn sm" disabled={!reason.trim() || busy} onClick={() => onConfirm("G6", "override", "Override recorded. Re-checking.", { reason })}>Override with reason</button></div>}
      {r.answer && <div className="small" style={{ marginTop: 12 }}>You answered: "{r.answer}". On record as a declaration, not proof.</div>}
      {r.question && !["G2", "G3"].includes(r.needs_human) && status(r) !== "not_met" && <div className="ask-box"><div className="label">Question for you</div>
        <div>{r.question}</div>
        <textarea aria-label="Your answer" value={answer} onChange={(e) => setAnswer(e.target.value)} placeholder="Your answer is recorded as a declaration, not proof" />
        <div className="actions"><button className="btn primary sm" disabled={!answer.trim() || busy || !r.question_id} onClick={onAnswer}>Save answer</button>
          <Link className="btn sm" to="/profile">Add evidence instead</Link></div></div>}
      {r.override_status && <div className="small" style={{ marginTop: 12 }}>Overridden by {r.override_by}: "{r.override_reason}"</div>}

      {(consulted.length > 0 || steps.length > 0) && <details className="more">
        <summary>Documents consulted and how</summary>
        {consulted.map((x: any, i: number) => <button key={i} className="link-row" onClick={() => onSource({ passage_id: x.passage_id, page: x.page })}>
          <strong>{x.document}{x.section ? ` · ${x.section}` : ""}{x.page ? ` · p.${x.page}` : ""}</strong>
          <span className="small muted">Retrieved because {x.why.find((w: string) => /refers to|defined term|second search/.test(w))}.</span>
        </button>)}
        {r.reference_gaps?.length > 0 && <div className="small">Could not be read: {r.reference_gaps.join(", ")}.</div>}
        {steps.length > 0 && <ol className="checks">{steps.map((s, i) => <li key={i}>{s}</li>)}</ol>}
      </details>}
    </>
  );
}
