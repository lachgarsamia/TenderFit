import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useProfile } from "../App";
import { api, daysLeft, fmtDate, post, waitForJobs } from "../api";
import { Card, Empty, ErrorNote, EvidenceBadge, Fictional, OppStatus, Tag, Tile, VerdictBadge } from "../components/ui";

const LEVEL: Record<string, [string, string]> = { strong: ["Strong", "info"], partial: ["Partial", "warn"], low: ["Low", "bad"] };
const FEAS: Record<string, [string, string]> = { feasible: ["Feasible", "ok"], confirm: ["Confirm workload", "warn"],
  blocker: ["Blocker", "bad"], unknown: ["Unknown", ""] };
const ELIG_TONE: Record<string, string> = { blocker_found: "bad", provisional: "warn", not_assessed: "warn", needs_confirmation: "ok", no_blocker: "ok" };

function source(r: any) {
  return `${r.obligation === "mandatory" ? "Mandatory" : r.obligation === "scored" ? "Scored" : "Info"} · ${r.document}${r.section ? ` §${r.section.split(" ")[0]}` : ""}${r.page ? ` · p.${r.page}` : ""}`;
}

function steps(r: any): string[] {
  if (r.agent_steps?.length) {
    return r.agent_steps.map((s: any) => {
      const arg = s.input.query ?? s.input.label ?? s.input.attribute ?? (s.input.passage_id ? `passage ${s.input.passage_id}` : "");
      return { search_passages: `Searched documents for "${arg}"`, read_section: `Read ${arg || "the section"}`,
        open_attachment: `Opened ${arg}`, check_clarifications: `Checked clarifications for "${arg}"`,
        read_profile: `Checked your ${arg || "profile"} facts`, compare: "Ran a rule check",
        record_verdict: `Stopped: ${r.status.replace("_", " ")}`, raise_question: "Stopped: asked you a question" }[s.tool as string] ?? s.tool;
    });
  }
  const by = { rule: "Deterministic rule check", model: "One model verdict with verified citations", guard: "Model verdict corrected by the deterministic guard",
    budget: "Not assessed by the model: budget reached", agent: "Agent investigation" }[r.decided_by as string];
  return [by ?? r.decided_by, r.reason];
}

export default function Assessment() {
  const { lotId } = useParams();
  const { profile, refreshChanges } = useProfile();
  const [a, setA] = useState<any>(null);
  const [inv, setInv] = useState<any>(null);
  const [missing, setMissing] = useState(false);
  const [sel, setSel] = useState<number | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [answer, setAnswer] = useState("");
  const [reason, setReason] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);
  const [uploadRef, setUploadRef] = useState<number | null>(null);

  const load = async () => {
    setError(null);
    api(`/lots/${lotId}/documents`).then(setInv).catch(() => {});
    try {
      const d = await api(`/lots/${lotId}/assessment?profile_id=${profile!.id}`);
      setA(d); setMissing(false);
      setSel((s) => s ?? d.requirements.find((r: any) => r.status !== "met")?.id ?? d.requirements[0]?.id ?? null);
    } catch (e: any) {
      if (e.message.includes("no assessment")) { setMissing(true); setA(null); } else setError(e.message);
    }
  };
  useEffect(() => { setSel(null); load(); }, [lotId, profile?.id]);

  const act = async (label: string, fn: () => Promise<any>) => {
    setBusy(label); setError(null);
    try {
      const out = await fn();
      if (out?.jobs?.length) await waitForJobs(out.jobs);
      await load(); refreshChanges();
    } catch (e: any) { setError(e.message); } finally { setBusy(null); }
  };
  const confirm = (point: string, target_id: number, decision: string, extra: any = {}) =>
    act(point, () => post("/confirmations", { point, target_id, decision, user: "you", profile_id: profile!.id, ...extra }));
  const upload = (refId: number | null) => { setUploadRef(refId); fileRef.current?.click(); };
  const onFile = (f: File | undefined) => {
    if (!f) return;
    const fd = new FormData(); fd.append("file", f); if (uploadRef) fd.append("ref_id", String(uploadRef)); fd.append("user", "you");
    act("upload", () => api(`/lots/${lotId}/documents`, { method: "POST", body: fd }));
  };

  const docs = inv?.documents ?? [];
  const essential = docs.filter((d: any) => d.essential && d.kind !== "notice");
  const unread = essential.filter((d: any) => d.status !== "read" && d.status !== "confirmed_none");
  const r = a?.requirements.find((x: any) => x.id === sel);
  const lot = a?.lot;
  const days = daysLeft(lot?.deadline);

  return (
    <>
      <input type="file" ref={fileRef} hidden accept=".pdf,.docx" onChange={(e) => onFile(e.target.files?.[0])} />
      <div className="crumb"><Link to="/opportunities">Opportunities</Link> / {lot?.buyer ?? "…"}</div>
      <div className="head-row">
        <div>
          <div className="title-row">
            <h1>{lot ? `Lot ${lot.lot_no}${lot.title ? ` · ${lot.title}` : ""}` : `Lot ${lotId}`}</h1>
            {a?.fictional_notice && <Fictional>Fictional notice · scripted model responses</Fictional>}
          </div>
          {a && <div className="small muted" style={{ marginTop: 4 }}>
            {lot.opportunity} · {a.version?.notice_type} tender notice, v{a.version?.version_no} of {a.version?.versions} · Deadline {fmtDate(lot.deadline)} ·{" "}
            <OppStatus status={a.status.status} suffix={`checked ${fmtDate(a.status.last_checked_at)}`} />
            {days !== null && days >= 0 && ` · ${days} days left`}
          </div>}
        </div>
        {a && <div className="actions">
          {a.version?.source_url && <a className="btn" href={a.version.source_url} target="_blank" rel="noreferrer">↗ Source notice</a>}
          <button className="btn" disabled={!!busy} onClick={() => confirm("G1", Number(lotId), "not_for_us")}>Not for us</button>
          {a.decision?.decision === "pursue"
            ? <Link className="btn primary" to={`/lots/${lotId}/preparation`}>Preparation plan →</Link>
            : <button className="btn primary" disabled={!!busy} onClick={() => confirm("G1", Number(lotId), "pursue")}>Pursue this lot</button>}
        </div>}
      </div>
      <ErrorNote error={error} />
      {a?.decision?.decision === "not_for_us" && <div className="banner info"><div className="body">You marked this lot "Not for us". TenderFit never decides for you.</div></div>}
      {a && !a.on_current_version && <div className="banner warn"><span className="icon">!</span><div className="body"><strong>A newer notice version exists.</strong> This assessment was made on an earlier version and is being re-checked.</div></div>}

      {(a?.provisional || (!a && unread.length > 0)) && unread.length > 0 && (
        <div className="banner warn">
          <span className="icon">!</span>
          <div className="body">
            <strong>Provisional assessment: {essential.length - unread.length} of {essential.length} essential documents read</strong>
            <div className="small">No blocker is claimed in documents TenderFit could not read. {unread.map((d: any) => d.label).join(", ")}{" "}
              {unread.length === 1 ? "has" : "have"} not been read: {unread[0]?.note ?? "not retrievable"}.</div>
          </div>
          <div className="actions">
            <button className="btn primary" disabled={!!busy} onClick={() => upload(unread[0]?.id)}>Upload document</button>
            <button className="btn" disabled={!!busy} onClick={() => confirm("G7", unread[0].id, "none_issued")}>Buyer issued none</button>
          </div>
        </div>
      )}

      {missing && (
        <Card title="Not assessed yet">
          <p className="muted">TenderFit reads the documents it can reach, extracts requirements with quotes and compares them with {profile?.name}'s facts.
            Extraction and verdicts use the language model, so this needs <code>ANTHROPIC_API_KEY</code> in <code>.env</code>.</p>
          <button className="btn primary" disabled={!!busy} onClick={() => act("assess", () => post(`/lots/${lotId}/assess`, { profile_id: profile!.id }))}>
            {busy === "assess" ? "Assessing… (this can take a minute)" : "Assess this lot"}
          </button>
        </Card>
      )}

      {a && <>
        <Card>
          <div className="rec">
            <strong>{a.recommendation}</strong>
            {a.provisional && <span className="small muted">Requirements in unread documents are not yet known.</span>}
            {!a.provisional && a.dimensions.eligibility.state !== "blocker_found" && <span className="small muted">No mandatory blocker found.</span>}
          </div>
          <div className="tiles">
            <Tile label="Relevance" value={LEVEL[a.dimensions.relevance.level]?.[0]} tone={LEVEL[a.dimensions.relevance.level]?.[1]} detail={a.dimensions.relevance.detail} />
            <Tile label="Eligibility" value={a.dimensions.eligibility.label} tone={ELIG_TONE[a.dimensions.eligibility.state]} detail={a.dimensions.eligibility.detail} />
            <Tile label="Evidence" value={a.dimensions.evidence.gaps ? `${a.dimensions.evidence.gaps} gap${a.dimensions.evidence.gaps > 1 ? "s" : ""}${a.provisional ? " so far" : ""}` : "No gaps"}
                  tone={a.dimensions.evidence.gaps ? "warn" : "ok"} detail={a.dimensions.evidence.detail} />
            <Tile label="Feasibility" value={FEAS[a.dimensions.feasibility.level]?.[0]} tone={FEAS[a.dimensions.feasibility.level]?.[1]} detail={a.dimensions.feasibility.detail} />
            <Tile label="Open questions" value={`${a.dimensions.open_questions.count} for you`} tone="info" detail={a.dimensions.open_questions.items[0]} />
          </div>
        </Card>

        <div className="grid-main">
          <Card title="Requirements and your evidence" extra={`${a.requirements.length} extracted · each linked to its source passage`}>
            {a.requirements.length === 0 ? <Empty>No requirements were found in the documents read.</Empty> : (
              <table>
                <thead><tr><th>Requirement · source</th><th>What we know about you</th><th>Status</th></tr></thead>
                <tbody>
                  {a.requirements.map((x: any) => (
                    <tr key={x.id} className={`clickable ${x.id === sel ? "selected" : ""}`} onClick={() => { setSel(x.id); setAnswer(""); setReason(""); }}>
                      <td><div style={{ fontWeight: 500 }}>{x.statement}</div><div className="mono">{source(x)}</div></td>
                      <td className="small">{x.facts.length ? x.facts.map((f: any) => f.label).join("; ") : <span className="muted">Not in your profile</span>}
                        {x.facts.some((f: any) => f.evidence_status === "declared") && <div className="muted">(declared)</div>}</td>
                      <td>
                        <VerdictBadge status={x.override_status ?? x.status} small />
                        <div className="small muted" style={{ marginTop: 3 }}>
                          {x.override_status ? "overridden by you" : x.basis === "declared" ? "on declared facts" : x.basis === "documents" ? "on documents" :
                            x.obligation === "scored" ? "affects score only" : x.needs_human === "G3" ? "workload" : x.needs_human === "G2" ? "evidence missing" :
                            x.status === "unknown" ? "not proven either way" : ""}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Card>

          <Card title={r ? `Evidence · ${r.category}` : "Evidence"}>
            {!r ? <Empty>Select a requirement.</Empty> : <>
              <div className="quote"><span className="mono">{r.document}{r.section ? ` · §${r.section}` : ""}{r.page ? ` · p.${r.page}` : ""}</span>"{r.quote}"</div>
              {r.citations.filter((c: any) => c.quote !== r.quote).map((c: any, i: number) => (
                <div className="quote" style={{ marginTop: 6 }} key={i}><span className="mono">also cited · {c.section ?? ""}{c.page ? ` · p.${c.page}` : ""}</span>"{c.quote}"</div>
              ))}
              <div className="section-label">How TenderFit checked</div>
              <ol className="steps">{steps(r).map((s, i) => <li key={i}>{s}</li>)}</ol>
              {r.facts.length > 0 && <>
                <div className="section-label">Profile facts used</div>
                {r.facts.map((f: any) => <div className="fact" key={f.id}><span className="small">{f.label}</span><EvidenceBadge status={f.evidence_status} /></div>)}
              </>}
              {r.escalate_reason && <div className="small muted" style={{ marginTop: 8 }}>Escalation: {r.escalate_reason}</div>}
              {r.override_status && <div className="small" style={{ marginTop: 8 }}>Override by {r.override_by}: "{r.override_reason}"</div>}

              {r.question && (
                <div className="question">
                  <div className="q-label">Question for you</div>
                  <div>{r.question}</div>
                  <textarea value={answer} onChange={(e) => setAnswer(e.target.value)} placeholder="Your answer (recorded as a declaration, not proof)" />
                  <div className="actions">
                    <button className="btn primary sm" disabled={!answer || !!busy || !r.question_id}
                            onClick={() => act("answer", () => post(`/questions/${r.question_id}/answer`, { answer, user: "you" }))}>Answer</button>
                    <Link className="btn sm" to="/profile">Upload evidence</Link>
                  </div>
                </div>
              )}
              {r.needs_human === "G2" && <button className="btn sm" style={{ marginTop: 10 }} disabled={!!busy}
                onClick={() => confirm("G2", r.id, "confirm_declaration")}>Confirm the declaration for this lot (G2)</button>}
              {r.needs_human === "G3" && <button className="btn sm" style={{ marginTop: 10 }} disabled={!!busy}
                onClick={() => confirm("G3", r.id, "confirm_for_lot")}>Confirm current workload for this lot (G3)</button>}
              {r.status === "not_met" && !r.override_status && (
                <div className="question">
                  <div className="q-label">Blocker · G6</div>
                  <div className="small">You can override a blocker only with a written reason, which is kept.</div>
                  <textarea value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Why this is not a blocker" />
                  <button className="btn sm" disabled={!reason.trim() || !!busy} onClick={() => confirm("G6", r.id, "override", { reason })}>Override with reason</button>
                </div>
              )}
            </>}
          </Card>
        </div>
      </>}

      <Card title="Document coverage" extra={inv && `${docs.filter((d: any) => d.kind !== "notice").length} documents referenced · ${essential.length} essential · coverage ${inv.coverage.replace("_", " ")}`}>
        {!inv ? <Empty>Loading…</Empty> : (
          <table>
            <thead><tr><th>Document</th><th>Referenced in</th><th>Access</th><th>Status</th><th></th></tr></thead>
            <tbody>
              {docs.map((d: any) => (
                <tr key={d.id} className={d.essential && d.status !== "read" && d.status !== "confirmed_none" ? "unread" : ""}>
                  <td><div style={{ fontWeight: 500 }}>{d.label === d.ocds_type && d.filename ? d.filename : d.label}</div>
                    <div className="mono">{d.filename && d.filename !== d.label ? `${d.filename} · ` : ""}{d.essential ? "essential" : "supporting"}</div></td>
                  <td className="small">{d.referenced_in ?? "—"}</td>
                  <td className="small">{d.kind === "notice" ? "Find a Tender" : d.origin === "upload" ? "Uploaded" : d.status === "read" ? "Public link" : d.url ? "Buyer portal or blocked" : "No link"}</td>
                  <td>
                    {d.status === "read" ? <Tag tone="ok">✓ Read</Tag> : d.status === "confirmed_none" ? <Tag tone="muted">Buyer issued none</Tag> :
                      d.status === "unreadable" ? <Tag tone="bad">✕ Unreadable</Tag> : d.essential ? <Tag tone="bad">✕ Not read</Tag> : <Tag tone="muted">Not read</Tag>}
                    <div className="small muted" style={{ marginTop: 3 }}>{d.status === "read" ? `${d.passages} passages` : d.status === "confirmed_none" ? "confirmed, no longer blocks a verdict" : d.essential ? "blocks a final verdict" : "you prepare it"}</div>
                  </td>
                  <td>{d.kind !== "notice" && d.status !== "read" && <button className="btn sm" disabled={!!busy} onClick={() => upload(d.id)}>Upload</button>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {a?.provisional && <div className="small muted" style={{ marginTop: 10 }}>
          What "provisional" means: requirements in unread documents count as unknown, never as met; "no blocker found" is shown only when every
          essential document has been read; an uploaded document is indexed and the lot is reassessed in full.</div>}
      </Card>
    </>
  );
}
