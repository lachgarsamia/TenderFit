/** Plain words for what the server reports. Nothing here adds facts: every sentence restates a field the API returned. */
import type { Change, Job, Outcome, Simulation, Step, Trace } from "./api";

export const INPUT: Record<string, string> = {
  overrides: "your overrides", answers: "your answers", requirements: "the requirement records",
  documents: "the tender documents", rejections: "requirements found but not extracted",
  baseline_record: "the assessment record", all: "all live inputs",
};

/** The investigation's state, from the job (if any), the simulation and the agent's outcome. */
export type Phase =
  | { kind: "queued"; waitingBehind?: number }
  | { kind: "running" }
  | { kind: "answered" }
  | { kind: "ended"; title: string; detail: string; tone: "warn" | "info" }
  | { kind: "failed"; title: string; detail: string }
  | { kind: "interrupted"; detail: string }
  | { kind: "discarded" }
  | { kind: "manual" };

const WORKER_STOPPED = /worker stopped while running/i;

export function phaseOf(sim: Simulation, trace: Trace | null, job: Job | null): Phase {
  if (sim.status === "discarded") return { kind: "discarded" };
  const status = job?.status ?? trace?.job?.status;
  if (!trace?.job && !(trace?.steps.length)) {
    if (sim.status === "failed" && sim.failure?.stage === "interrupted") return { kind: "interrupted", detail: "The run stopped before it finished, so it was closed. Nothing live was changed." };
    if (sim.status === "failed") return { kind: "failed", title: "The simulation could not be completed", detail: sim.failure?.message ?? "" };
    return { kind: "manual" };
  }
  if (status === "queued") return { kind: "queued", waitingBehind: job?.waiting_behind };
  if (status === "running") return { kind: "running" };
  if (status === "failed") {
    const why = job?.last_error ?? "";
    return WORKER_STOPPED.test(why)
      ? { kind: "interrupted", detail: "The investigation stopped part-way because TenderFit's background worker restarted. It is not retried automatically, so that it never spends twice. You can ask again." }
      : { kind: "failed", title: "The investigation failed", detail: "Something went wrong on TenderFit's side. It is not retried automatically; you can ask again." };
  }
  if (sim.status === "failed") {
    return sim.failure?.stage === "interrupted"
      ? { kind: "interrupted", detail: "The simulation run stopped before it finished, so it was closed. Nothing live was changed." }
      : { kind: "failed", title: "The simulation could not be completed", detail: sim.failure?.message ?? "" };
  }
  const o = trace?.outcome;
  if (!o) return status === "done" ? { kind: "ended", title: "The investigation ended without an answer", detail: "No answer was recorded.", tone: "warn" } : { kind: "queued" };
  if (o.outcome === "answered") return { kind: "answered" };
  if (o.outcome === "unsupported") return { kind: "ended", tone: "info", title: "This question can't be simulated",
    detail: `The rules only test what the tender's requirements check. ${o.reason ? `The agent said: ${o.reason}` : ""}`.trim() };
  if (o.outcome === "agent_unavailable") return { kind: "ended", tone: "warn", title: "The AI service was unavailable",
    detail: "The investigation could not reach the AI model, so it stopped. Nothing was guessed." };
  if (o.outcome === "budget") return { kind: "ended", tone: "warn", title: "The investigation stopped at a limit",
    detail: limitReason(o.reason) };
  return { kind: "ended", tone: "warn", title: "The investigation ended", detail: o.reason ?? "" };
}

function limitReason(reason?: string): string {
  if (!reason) return "It stopped before giving an answer.";
  if (/time limit/i.test(reason)) return "It reached its time limit (about 2½ minutes) before giving an answer. Nothing was guessed.";
  if (/daily allowance/i.test(reason)) return "Your daily AI allowance would have been exceeded, so it stopped before the next step. Nothing was guessed.";
  if (/question's allowance|cost cap/i.test(reason)) return "This question reached its spending limit before an answer. Nothing was guessed.";
  if (/tool-call limit|step limit/i.test(reason)) return "It used all the steps it is allowed before giving an answer. Nothing was guessed.";
  return reason;
}

/** One hypothetical change in plain words. `statement` names a requirement when the page knows it. */
export function describeChange(c: Change, statement?: (id: number) => string | undefined): string {
  const req = (id: unknown) => statement?.(Number(id)) ? `"${statement(Number(id))}"` : `requirement ${id}`;
  switch (c.kind) {
    case "set_condition_bound": return `${req(c.requirement_id)}: the limit becomes ${c.bound}`;
    case "set_condition_match": return `${req(c.requirement_id)}: accepts ${String(c.match)}`;
    case "set_requirement_text": return `${req(c.requirement_id)}: reworded as "${String(c.statement)}"`;
    case "set_fact_validity": return `Profile fact ${c.fact_id}: valid until ${c.valid_until ?? "no expiry"}`;
    case "set_fact_evidence": return `Profile fact ${c.fact_id}: evidence becomes ${String(c.evidence_status).replace(/_/g, " ")}`;
    case "set_fact_value": return `Profile fact ${c.fact_id}: ${String(c.key).replace(/_/g, " ")} becomes ${JSON.stringify(c.value)}`;
    case "remove_fact": return `Profile fact ${c.fact_id} is taken away`;
    case "add_fact": return `A new ${String(c.attribute)} fact is added: ${String(c.label)}`;
    case "set_notice_field": return c.field === "notice.deadline" ? `The tender deadline becomes ${c.value}` : `The lot quantity becomes ${c.value}`;
    default: return c.kind.replace(/_/g, " ");
  }
}

function parseChange(text: unknown): Change | null {
  try {
    const c = JSON.parse(String(text));
    return c && typeof c === "object" && typeof c.kind === "string" ? c : null;
  } catch { return null; }
}

/** One recorded agent step, for a supplier: what it did, what it found, and whether it failed. */
export function describeStep(s: Step, statement?: (id: number) => string | undefined): { title: string; detail?: string; problem?: string } {
  const err = typeof s.output?.error === "string" ? s.output.error : undefined;
  switch (s.tool) {
    case "question": return { title: "Your question", detail: String(s.input.question ?? "") };
    case "read_baseline": return { title: "Read the frozen copy of your assessment",
      detail: Array.isArray(s.output.requirements) ? `${s.output.requirements.length} requirements` : undefined };
    case "read_facts": return { title: `Looked at your profile facts${s.input.attribute ? ` (${s.input.attribute})` : ""}`,
      detail: Array.isArray(s.output.facts) ? `${s.output.facts.length} found` : undefined };
    case "add_change": {
      const c = parseChange(s.input.change);
      return { title: "Proposed a hypothetical change", detail: c ? describeChange(c, statement) : undefined,
        problem: err ? `Not accepted: ${err}` : undefined };
    }
    case "preview": return { title: "Checked which requirements the changes reach",
      detail: Array.isArray(s.output.reached) ? `${s.output.reached.length} reached, ${s.output.not_reached?.length ?? 0} not reached` : undefined, problem: err };
    case "run_simulation": return { title: "Ran the rules with and without the changes",
      detail: s.output.summary ? `${s.output.summary.changed?.length ?? 0} verdict${s.output.summary.changed?.length === 1 ? "" : "s"} changed` : undefined, problem: err };
    case "finish": return err ? { title: "Drafted an answer", problem: `Sent back: ${err}` } : { title: "Wrote the answer, checked against the results" };
    case "cannot_simulate": return { title: "Decided the question can't be simulated", detail: String(s.input.reason ?? "") };
    case "no_tool": return { title: "Replied without taking a step", detail: "It was asked to use one of its tools." };
    case "llm_error": return { title: "The AI call failed", problem: String(s.output.error ?? "") };
    case "end": return { title: "Stopped", detail: s.output.reason ? String(s.output.reason) : undefined };
    default: return { title: s.tool.replace(/_/g, " "), problem: err };
  }
}

export const usd = (v: number) => (v > 0 && v < 0.01 ? "under $0.01" : `$${v.toFixed(2)}`);
