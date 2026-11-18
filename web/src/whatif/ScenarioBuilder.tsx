import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { usePageTitle } from "../App";
import { STATIC, fmtDate } from "../api";
import { Card, ErrorNote, Skeleton, Tag } from "../components/ui";
import * as sims from "./api";
import type { Change, InputFact, Inputs } from "./api";
import { HypotheticalBanner, SimVerdict } from "./SimulationPage";
import { describeChange, evidenceLabel } from "./describe";

/** The change kinds a supplier builds by hand. Rewording a requirement is left out: rules cannot read wording, so such a
 * scenario could only ever say "needs reading". */
export const KIND_LABEL: Record<string, string> = {
  set_condition_bound: "Change a requirement's limit",
  set_condition_match: "Change the standard or document a requirement asks for",
  set_fact_value: "Change a value in one of your profile facts",
  set_fact_validity: "Change when a profile fact expires",
  set_fact_evidence: "Change how a profile fact is evidenced",
  remove_fact: "Suppose you no longer had a profile fact",
  add_fact: "Suppose you had a fact you don't hold today",
  set_notice_field: "Change the tender's deadline or quantity",
};
const MAX_CHANGES = 25;

type Props = { lotId: string; inputs: Inputs; parentId?: number; initial: Change[] };

/** Build a scenario by hand: choose a change, its target and its value; review the list; create it (and run it). Every
 * value shown as "now" is the actual, frozen value; every value in the list is hypothetical. Nothing is sent until
 * Create, and the server checks every change again. */
export default function ScenarioBuilderPage() {
  const { lotId = "" } = useParams();
  const [params] = useSearchParams();
  const assessment = Number(params.get("assessment"));
  const from = Number(params.get("from"));
  const [inputs, setInputs] = useState<Inputs | null>(null);
  const [error, setError] = useState<string | null>(null);
  usePageTitle(from ? "New variation" : "New scenario");

  useEffect(() => {
    let cancelled = false;
    setInputs(null); setError(null);
    if (STATIC) { setError("Scenarios are switched off in the read-only demo."); return; }
    const load = from ? sims.inputsForSimulation(from) : assessment ? sims.inputsForAssessment(lotId, assessment) : null;
    if (!load) { setError("Open this page from an assessment or a scenario."); return; }
    load.then((i) => { if (!cancelled) setInputs(i); }).catch((e) => { if (!cancelled) setError(sims.refusal(e).message); });
    return () => { cancelled = true; };
  }, [lotId, assessment, from]);

  return <div className="hypothetical-page">
    <div className="crumb">{from ? <Link to={`/lots/${lotId}/simulations/${from}`}>← Back to the scenario</Link>
      : <Link to={`/lots/${lotId}`}>← Back to your assessment</Link>}</div>
    <HypotheticalBanner />
    <div className="page-head"><div style={{ minWidth: 0 }}>
      <div className="title-row"><span className="pill sm hypo-tag">What if</span><h1>{from ? "Try a variation" : "Build a scenario"}</h1></div>
      <div className="meta">{from ? <span>Starts from the same frozen copy as <Link to={`/lots/${lotId}/simulations/${from}`}>scenario {from}</Link>, with its changes ready to edit.</span>
        : <span>Starts from a frozen copy of <Link to={`/lots/${lotId}`}>your assessment</Link>.</span>}</div>
    </div></div>
    {error && <ErrorNote error={error} />}
    {!error && !inputs && <Card><Skeleton lines={5} /></Card>}
    {inputs && <Builder lotId={lotId} inputs={inputs} parentId={from || undefined} initial={inputs.changes ?? []} />}
  </div>;
}

export function Builder({ lotId, inputs, parentId, initial }: Props) {
  const navigate = useNavigate();
  const [draft, setDraft] = useState<Change[]>(initial);
  const [title, setTitle] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const statement = (id: number) => inputs.requirements.find((r) => r.id === id)?.statement;
  const fact = (id: number) => inputs.facts.find((f) => f.id === id)?.label ?? (id < 0 ? addedLabel(draft, id) : undefined);
  const full = draft.length >= MAX_CHANGES;

  const submit = async (andRun: boolean) => {
    setBusy(true); setError(null);
    try {
      const body = { changes: draft, ...(title.trim() ? { title: title.trim() } : {}) };
      const sim = parentId ? await sims.createVariation(parentId, body) : await sims.create(inputs.assessment_id, body);
      if (andRun) {
        try { await sims.run(sim.id); } catch { /* the scenario exists; its page shows why the run did not complete */ }
      }
      navigate(`/lots/${lotId}/simulations/${sim.id}`);
    } catch (e) {
      const r = sims.refusal(e);
      setError(r.kind === "invalid" ? `The server did not accept this scenario: ${r.message}. Nothing was created.` : r.message);
      setBusy(false);
    }
  };

  return <>
    <Card title="Add a hypothetical change" note={full ? `At most ${MAX_CHANGES} changes` : "Nothing is sent until you create the scenario"}>
      {full ? <p className="small">This scenario has as many changes as it can hold. Remove one to add another.</p>
        : <ChangeForm inputs={inputs} draft={draft} onAdd={(c) => setDraft([...draft, c])} />}
    </Card>

    <Card title="Review the hypothesis" note={`${draft.length} change${draft.length === 1 ? "" : "s"} · applied to a frozen copy only`}>
      {draft.length === 0 ? <p className="small muted" style={{ marginTop: 0 }}>No changes yet. A scenario with no changes is allowed: it shows your assessment re-checked as it stands today.</p> :
        <ol className="hypothesis draft" aria-label="Draft changes">{draft.map((c, i) => <li key={i}>
          <span className="pill sm hypo">Hypothetical</span> <span>{describeChange(c, statement, fact)}</span>
          {c.kind === "add_fact" && <div className="small warn-text">You don't hold this today. The result only says what would follow if you did.</div>}
          <button type="button" className="btn sm" aria-label={`Remove change ${i + 1}`} disabled={busy}
            onClick={() => setDraft(draft.filter((_, j) => j !== i))}>Remove</button>
        </li>)}</ol>}
      <label className="field"><span>Name (optional)</span>
        <input value={title} maxLength={500} onChange={(e) => setTitle(e.target.value)} placeholder="For example: 30-day delivery" /></label>
      <ErrorNote error={error} />
      <div className="actions" style={{ marginTop: 12 }}>
        <button className="btn primary" disabled={busy} onClick={() => submit(true)}>{busy ? "Working…" : "Create and run"}</button>
        <button className="btn" disabled={busy} onClick={() => submit(false)}>Create without running</button>
      </div>
      <p className="small muted">Running uses the rules only and no AI, so it costs nothing. Requirements the rules can't decide are marked "needs reading", never guessed.</p>
    </Card>
  </>;
}

function addedLabel(draft: Change[], id: number) {
  const added = draft.filter((c) => c.kind === "add_fact");
  return added[-id - 1] ? `${String(added[-id - 1].label)} (hypothetical)` : undefined;
}

/** One change at a time. The form only offers targets the change can apply to, and shows each target's actual value. */
export function ChangeForm({ inputs, draft, onAdd }: { inputs: Inputs; draft: Change[]; onAdd: (c: Change) => void }) {
  const [kind, setKind] = useState("set_condition_bound");
  const [target, setTarget] = useState("");
  const [value, setValue] = useState("");
  const [key, setKey] = useState("");
  const [attr, setAttr] = useState("certificate");
  const [label, setLabel] = useState("");
  const [values, setValues] = useState<Record<string, string>>({});
  const [evidence, setEvidence] = useState("declared");
  const [validUntil, setValidUntil] = useState("");
  const [problem, setProblem] = useState<string | null>(null);

  const conditions = useMemo(() => inputs.requirements.flatMap((r) => r.conditions
    .filter((c) => c.accepts.includes(kind as never)).map((c) => ({ r, c, id: `${r.id}:${c.ordinal}` }))), [inputs, kind]);
  // Facts added earlier in this draft can be edited too; they are numbered -1, -2, ... as the server numbers them.
  const added: InputFact[] = draft.filter((c) => c.kind === "add_fact").map((c, i) => ({
    id: -1 - i, attribute: String(c.attribute), label: `${String(c.label)} (hypothetical)`, value: (c.value as Record<string, unknown>) ?? {},
    evidence_status: String(c.evidence_status ?? "declared"), valid_until: (c.valid_until as string) ?? null,
    editable_keys: Object.keys(inputs.fact_kinds[String(c.attribute)]?.keys ?? {}),
  }));
  const facts = [...inputs.facts, ...added];
  const chosenFact = facts.find((f) => String(f.id) === target);
  const chosenCond = conditions.find((x) => x.id === target);

  useEffect(() => { setTarget(""); setValue(""); setKey(""); setProblem(null); }, [kind]);
  useEffect(() => { setValue(""); setKey(""); setProblem(null); }, [target]);

  const reset = () => { setTarget(""); setValue(""); setKey(""); setLabel(""); setValues({}); setValidUntil(""); setProblem(null); };
  const add = (c: Change | string) => { if (typeof c === "string") { setProblem(c); return; } onAdd(c); reset(); };

  const build = (): Change | string => {
    switch (kind) {
      case "set_condition_bound": {
        if (!chosenCond) return "Choose a requirement.";
        const n = Number(value);
        if (value.trim() === "" || !Number.isFinite(n) || n < 0) return "Enter a number of zero or more.";
        return { kind, requirement_id: chosenCond.r.id, condition: chosenCond.c.ordinal, bound: n };
      }
      case "set_condition_match":
        if (!chosenCond) return "Choose a requirement.";
        if (!value.trim()) return "Name the standard or document.";
        return { kind, requirement_id: chosenCond.r.id, condition: chosenCond.c.ordinal, match: value.trim() };
      case "set_fact_value": {
        if (!chosenFact) return "Choose a profile fact.";
        if (!key) return "Choose which value to change.";
        const v = typed(inputs.fact_kinds[chosenFact.attribute]?.keys[key], value);
        return typeof v === "string" && v.startsWith("!") ? v.slice(1) : { kind, fact_id: chosenFact.id, key, value: v };
      }
      case "set_fact_validity":
        if (!chosenFact) return "Choose a profile fact.";
        return { kind, fact_id: chosenFact.id, valid_until: value || null };
      case "set_fact_evidence":
        if (!chosenFact) return "Choose a profile fact.";
        if (!value) return "Choose how it is evidenced.";
        return { kind, fact_id: chosenFact.id, evidence_status: value };
      case "remove_fact":
        if (!chosenFact) return "Choose a profile fact.";
        return { kind, fact_id: chosenFact.id };
      case "add_fact": {
        if (!label.trim()) return "Describe the fact.";
        const spec = inputs.fact_kinds[attr];
        const v: Record<string, unknown> = {};
        for (const [k, t] of Object.entries(spec?.keys ?? {})) {
          if (!(values[k] ?? "").trim()) continue;
          const x = typed(t, values[k]);
          if (typeof x === "string" && x.startsWith("!")) return `${k.replace(/_/g, " ")}: ${x.slice(1)}`;
          v[k] = x;
        }
        if (spec?.required && v[spec.required] === undefined) return `Give the ${spec.required.replace(/_/g, " ")}.`;
        return { kind, attribute: attr, label: label.trim(), value: v, evidence_status: evidence, ...(validUntil ? { valid_until: validUntil } : {}) };
      }
      case "set_notice_field": {
        if (target !== "notice.deadline" && target !== "lot.quantity") return "Choose the deadline or the quantity.";
        if (target === "notice.deadline") return value ? { kind, field: target, value } : "Choose a date.";
        const n = Number(value);
        return value.trim() !== "" && Number.isFinite(n) && n > 0 ? { kind, field: target, value: n } : "Enter a quantity above zero.";
      }
    }
    return "Choose a kind of change.";
  };

  const factPicker = <label className="field"><span>Profile fact</span>
    <select value={target} onChange={(e) => setTarget(e.target.value)}>
      <option value="">Choose…</option>
      {facts.map((f) => <option key={f.id} value={f.id}>{f.label} · {f.attribute.replace(/_/g, " ")}</option>)}
    </select></label>;

  return <form className="change-form" onSubmit={(e) => { e.preventDefault(); add(build()); }} aria-label="Add a hypothetical change">
    <label className="field"><span>Kind of change</span>
      <select value={kind} onChange={(e) => setKind(e.target.value)}>
        {Object.entries(KIND_LABEL).map(([k, l]) => <option key={k} value={k}>{l}</option>)}
      </select></label>

    {(kind === "set_condition_bound" || kind === "set_condition_match") && <>
      {conditions.length === 0 ? <p className="small muted">No requirement in this assessment has a {kind === "set_condition_bound" ? "numeric limit" : "named standard or document"} that can be changed.</p> : <>
        <label className="field"><span>Requirement</span>
          <select value={target} onChange={(e) => setTarget(e.target.value)}>
            <option value="">Choose…</option>
            {conditions.map(({ r, c, id }) => <option key={id} value={id}>{r.statement}{r.conditions.length > 1 ? ` · ${c.label ?? `check ${c.ordinal}`}` : ""}</option>)}
          </select></label>
        {chosenCond && <Actual>
          The tender asks for: <strong>{kind === "set_condition_bound" ? `${chosenCond.c.bound}${chosenCond.c.unit ? ` ${chosenCond.c.unit}` : ""}` : chosenCond.c.match}</strong>
          {" "}· your assessment: <SimVerdict status={chosenCond.r.baseline_status} real /></Actual>}
        <label className="field"><span>{kind === "set_condition_bound" ? "Suppose the limit were" : "Suppose it accepted"}</span>
          <input value={value} onChange={(e) => setValue(e.target.value)} inputMode={kind === "set_condition_bound" ? "decimal" : undefined}
            maxLength={200} /></label>
      </>}
    </>}

    {kind === "set_fact_value" && <>
      {factPicker}
      {chosenFact && <>
        <label className="field"><span>Value to change</span>
          <select value={key} onChange={(e) => setKey(e.target.value)}>
            <option value="">Choose…</option>
            {chosenFact.editable_keys.map((k) => <option key={k} value={k}>{k.replace(/_/g, " ")}</option>)}
          </select></label>
        {key && <Actual>Now: <strong>{show(chosenFact.value[key])}</strong></Actual>}
        {key && <label className="field"><span>Suppose it were{inputs.fact_kinds[chosenFact.attribute]?.keys[key] === "list" ? " (separate with commas)" : ""}</span>
          <ValueInput type={inputs.fact_kinds[chosenFact.attribute]?.keys[key]} value={value} onChange={setValue} /></label>}
      </>}
    </>}

    {kind === "set_fact_validity" && <>
      {factPicker}
      {chosenFact && <Actual>Now valid until: <strong>{chosenFact.valid_until ? fmtDate(chosenFact.valid_until, false) : "no expiry date on record"}</strong></Actual>}
      <label className="field"><span>Suppose it were valid until (leave empty for no expiry)</span>
        <input type="date" value={value} onChange={(e) => setValue(e.target.value)} /></label>
    </>}

    {kind === "set_fact_evidence" && <>
      {factPicker}
      {chosenFact && <Actual>Now: <strong>{evidenceLabel(chosenFact.evidence_status)}</strong></Actual>}
      <label className="field"><span>Suppose it were</span>
        <select value={value} onChange={(e) => setValue(e.target.value)}>
          <option value="">Choose…</option>
          {inputs.evidence_statuses.existing.map((s) => <option key={s} value={s}>{evidenceLabel(s)}</option>)}
        </select></label>
      {(value === "document_on_file" || value === "declared") && <p className="small warn-text">This supposes evidence you may not hold. Upload or declare it on your Profile to make it real.</p>}
    </>}

    {kind === "remove_fact" && <>
      {factPicker}
      {chosenFact && <Actual>Now: <strong>{chosenFact.label}</strong> · {evidenceLabel(chosenFact.evidence_status)}</Actual>}
    </>}

    {kind === "add_fact" && <>
      <div className="banner warn compact" role="note" aria-label="Hypothetical evidence"><span className="b-icon">!</span><div className="body">
        <strong>Hypothetical evidence</strong><p>A fact added here exists only in this scenario. It is never added to your profile, and it does not count as evidence you hold.</p></div></div>
      <label className="field"><span>Kind of fact</span>
        <select value={attr} onChange={(e) => { setAttr(e.target.value); setValues({}); }}>
          {Object.keys(inputs.fact_kinds).map((a) => <option key={a} value={a}>{a.replace(/_/g, " ")}</option>)}
        </select></label>
      <label className="field"><span>Short description</span><input value={label} maxLength={200} onChange={(e) => setLabel(e.target.value)} placeholder="For example: ISO 14001 certificate" /></label>
      {Object.entries(inputs.fact_kinds[attr]?.keys ?? {}).map(([k, t]) => <label className="field" key={k}>
        <span>{k.replace(/_/g, " ")}{inputs.fact_kinds[attr]?.required === k ? " (required)" : ""}{t === "list" ? " (separate with commas)" : ""}</span>
        <ValueInput type={t} value={values[k] ?? ""} onChange={(v) => setValues({ ...values, [k]: v })} /></label>)}
      <label className="field"><span>Suppose it were</span>
        <select value={evidence} onChange={(e) => setEvidence(e.target.value)}>
          {inputs.evidence_statuses.added.map((s) => <option key={s} value={s}>{evidenceLabel(s)}</option>)}
        </select></label>
      <label className="field"><span>Valid until (optional)</span><input type="date" value={validUntil} onChange={(e) => setValidUntil(e.target.value)} /></label>
    </>}

    {kind === "set_notice_field" && <>
      <label className="field"><span>Tender field</span>
        <select value={target} onChange={(e) => setTarget(e.target.value)}>
          <option value="">Choose…</option>
          <option value="notice.deadline">Submission deadline</option>
          <option value="lot.quantity">Lot quantity</option>
        </select></label>
      {target && <Actual>The tender says: <strong>{show(inputs.fields[target as "notice.deadline"]) || "not stated"}</strong></Actual>}
      {target && <label className="field"><span>Suppose it were</span>
        {target === "notice.deadline" ? <input type="date" value={value} onChange={(e) => setValue(e.target.value)} />
          : <input value={value} inputMode="numeric" onChange={(e) => setValue(e.target.value)} />}</label>}
    </>}

    {problem && <p className="small bad-text" role="alert">{problem}</p>}
    <div className="actions"><button className="btn" type="submit">Add to the scenario</button></div>
  </form>;
}

function Actual({ children }: { children: React.ReactNode }) {
  return <div className="actual small"><Tag tone="outline">Actual</Tag> {children}</div>;
}

function ValueInput({ type, value, onChange }: { type?: string; value: string; onChange: (v: string) => void }) {
  if (type === "boolean") return <select value={value} onChange={(e) => onChange(e.target.value)}>
    <option value="">Choose…</option><option value="true">Yes</option><option value="false">No</option></select>;
  return <input value={value} maxLength={500} inputMode={type === "number" ? "decimal" : undefined} onChange={(e) => onChange(e.target.value)} />;
}

/** The value as the server expects it; a string starting with "!" is a problem to show instead. */
function typed(type: string | undefined, raw: string): unknown {
  if (type === "number") {
    const n = Number(raw);
    return raw.trim() !== "" && Number.isFinite(n) ? n : "!enter a number";
  }
  if (type === "boolean") return raw === "true" ? true : raw === "false" ? false : "!choose yes or no";
  if (type === "list") {
    const items = raw.split(",").map((x) => x.trim()).filter(Boolean);
    return items.length ? items : "!give at least one item";
  }
  return raw.trim() ? raw.trim() : "!enter a value";
}

function show(v: unknown): string {
  if (v === null || v === undefined || v === "") return "";
  return Array.isArray(v) ? v.join(", ") : String(v);
}
