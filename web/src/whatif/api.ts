/** Mode B over HTTP: typed calls to the what-if routes, and the one place their errors are classified.
 *
 * The shapes follow the server as built (tenderfit/api/simulations.py, GET /jobs/{id}); docs/openapi.json types these
 * responses only as objects, so they are written out here. The supplier is never sent: the server takes it from the
 * session. */
import { ApiError, api, post } from "../api";

export type Status = "met" | "not_met" | "needs_confirmation" | "unknown";
export type SimStatus = "created" | "running" | "completed" | "failed" | "discarded";
export type JobStatus = "queued" | "running" | "done" | "failed";

export type Allowance = {
  daily_usd: number; session_usd: number; spent_24h_usd: number; remaining_usd: number;
  can_ask: boolean; next_question_at: string | null; active: { simulation_id: number; job_id: number } | null;
};

export type Job = {
  id: number; kind: string; status: JobStatus; attempts: number; last_error: string | null;
  created_at: string; finished_at: string | null; waiting_behind?: number;
};

export type Change = { kind: string; [field: string]: unknown };

export type InputDrift = { input: string; requirement_id: number | null; detail: string };

export type Summary = {
  attribution?: { basis: string; decision_limit: number; decisions: number; limited: boolean };
  requirements: number;
  changed: { requirement_id: number; statement: string; obligation: string; from: Status; to: Status }[];
  needs_model: number[];
  not_affected: number[];
  drift: { requirement_id: number; baseline: Status; control: Status }[];
  stale_baseline: number[];
  input_drift: { since_creation: InputDrift[]; since_baseline: InputDrift[] };
  recommendation: { baseline: string | null; control: string; simulated: string; complete: boolean; drift: boolean };
  execution?: { frozen: string[]; live_inputs: Record<string, { changed: boolean; at_creation: string | null }> };
};

export type Simulation = {
  hypothetical: true; notice: string; id: number; lot_id: number; status: SimStatus; title: string | null; parent_id: number | null;
  baseline: { assessment_id: number; version_id: number; recommendation: string | null; superseded: boolean | null };
  as_of: string; drift: { facts_changed_since_baseline?: number[]; baseline_on_current_version?: boolean; baseline_superseded?: boolean; [k: string]: unknown };
  changes: Change[]; summary: Summary | null; failure: { stage: string | null; message: string } | null;
  created_by: string; created_at: string; started_at: string | null; finished_at: string | null; discarded_at: string | null;
};

export type Provenance = {
  citations: { passage_id: number; quote: string }[];
  facts: { id: number; label: string; origin: "actual" | "modified" | "hypothetical" | "removed";
    value: { [key: string]: unknown }; evidence_status: string; valid_until: string | null; declaration_confirmed: boolean;
    changes: { ordinal: number; kind: string; before: unknown; after: unknown }[];
    support: { evidence_file_id: number; page: number | null; quote: string | null; verified: boolean; human_verified: boolean; verification_level?: string | null }[] }[];
  params: { field: string; value: unknown; origin: string; changes: { ordinal: number; before: unknown; after: unknown }[] }[];
  requirement_assumptions: { ordinal: number; change: Change }[];
  needs_human: string | null; gaps: unknown[]; support_available: boolean;
};
export type ResolvedProvenance = {
  control: Provenance | null; simulated: Provenance | null;
  passages: Record<string, { filename: string; section: string | null; text: string } | null>;
  evidence: Record<string, { filename: string } | null>;
};

export type AttributionProbe = {
  state: "decided" | "needs_model" | "invalid_subset"; ordinals: number[]; status?: Status;
  reason?: string; detail?: string; provenance?: Provenance;
};
export type AttributionEntry = {
  ordinal: number; state: "computed" | "undetermined" | "limit" | "single_reaching_change";
  labels: ("sufficient_alone" | "necessary" | "jointly_required" | "redundant" | "no_independent_effect")[];
  detail?: string; alone?: AttributionProbe; without?: AttributionProbe;
};
export type Attribution = { basis: string; changes: AttributionEntry[] };

export type ResultRow = {
  requirement_id: number; statement: string; obligation: string;
  baseline_status: Status | null; control_status: Status | null; simulated_status: Status | null;
  attribution?: Attribution | null;
  provenance?: Provenance | null; control_provenance?: Provenance | null;
  how: "rule" | "needs_model" | "not_affected"; effect: boolean; drift: boolean;
  reached: { kind: string; target: string; detail: string }[];
  reason_control: string | null; reason_simulated: string | null; notes: string[]; input_drift: InputDrift[];
};

export type Outcome = {
  outcome: "answered" | "unsupported" | "budget" | "agent_unavailable" | string;
  simulation_id: number; cost_usd: number; tool_calls: number; reason?: string;
  answer?: string; changed?: Summary["changed"]; caveats?: string[];
  recommendation?: { before: string; after: string; complete: boolean };
};

export type Step = {
  step: number; tool: string; input: Record<string, any>; output: Record<string, any>; model: string | null;
  tokens: { input: number; output: number }; cost_usd: number; cost_estimated: boolean; created_at: string;
};

export type Trace = { simulation_id: number; job: { id: number; status: JobStatus } | null; outcome: Outcome | null; steps: Step[] };

export type ListedSimulation = {
  id: number; lot_id: number; baseline_assessment_id: number; status: SimStatus; title: string | null;
  as_of: string; created_by: string; created_at: string; changes: number; parent_id: number | null; snapshot_sha256: string;
};

/** What a scenario can change: the frozen, actual values. Nothing hypothetical is in here. */
export type InputCondition = {
  ordinal: number; label: string | null; mandatory: boolean; machine_checkable: boolean; kind: string | null; fact: string | null;
  bound: number | string | null; match: string | null; unit: string | null; accepts: ("set_condition_bound" | "set_condition_match")[];
};
export type InputRequirement = {
  id: number; statement: string; obligation: string; category: string; baseline_status: Status | null;
  quote: string | null; passage_id: number | null; conditions: InputCondition[];
};
export type InputFact = {
  id: number; attribute: string; label: string; value: Record<string, unknown>; evidence_status: string;
  valid_until: string | null; editable_keys: string[];
};
export type Inputs = {
  hypothetical: false; assessment_id: number; lot_id: number; as_of: string;
  requirements: InputRequirement[]; facts: InputFact[]; fields: { "notice.deadline"?: string | null; "lot.quantity"?: number | null };
  fact_kinds: Record<string, { keys: Record<string, "text" | "number" | "list" | "boolean">; required: string | null }>;
  evidence_statuses: { existing: string[]; added: string[] };
  simulation_id?: number; snapshot_sha256?: string; changes?: Change[];
};

export type Direction = "improved" | "worsened" | "unchanged" | "undetermined";
export type Cell = {
  attribution?: Attribution | null;
  control: Status | null; simulated: Status | null; how: ResultRow["how"]; direction: Direction; effect: boolean;
  reason_control: string | null; reason_simulated: string | null; reached: ResultRow["reached"]; notes: string[];
  drift: boolean; input_drift: InputDrift[];
};
export type ChangeReach = {
  counterfactuals?: (AttributionEntry & { requirement_id: number })[];
  ordinal: number; change: Change; target: string; reaches: number[]; moved: number[]; attributable: number[];
  shared_with_other_changes: number[]; undetermined: number[]; blockers_resolved: number[]; blockers_introduced: number[];
  uncertainties_resolved: number[]; no_effect: boolean; assumes_evidence: boolean;
};
export type Highlights = Record<"blockers_resolved" | "blockers_now_unknown" | "blockers_introduced" | "uncertainties_resolved" |
  "now_needs_confirmation" | "needs_model", number[]>;
export type ComparedScenario = {
  id: number; title: string | null; parent_id: number | null; as_of: string; snapshot_sha256: string; changes: ChangeReach[];
  recommendation: Summary["recommendation"] | null; counts: Record<Direction, number>; transitions: Record<string, number>;
  highlights: Highlights; no_effect: boolean; input_drift: Summary["input_drift"]; drift: number[];
};
export type PriorityEntry = Pick<ChangeReach, "ordinal" | "change" | "reaches" | "moved" | "attributable" | "blockers_resolved" |
  "blockers_introduced" | "uncertainties_resolved" | "assumes_evidence"> & { scenario_id: number };
export type Comparison = {
  hypothetical: true; notice: string; lot_id: number; baseline_assessment_id: number;
  integrity: { same_snapshot: boolean; same_live_inputs: boolean; consistent: boolean; notes: string[] };
  scenarios: ComparedScenario[];
  requirements: {
    requirement_id: number; statement: string; obligation: string; category: string; baseline_status: Status | null;
    quote: string | null; passage_id: number | null; scenarios: Record<string, Cell | null>; affected: boolean; differs_between_scenarios: boolean;
  }[];
  facts: Record<string, string>; order: number[]; ordering_rule: string; verdict_order: Status[];
  priorities: Record<"widest_reach" | "addresses_blockers" | "resolves_uncertainty" | "introduces_blockers" | "little_or_no_effect" |
    "depends_on_evidence_not_held", PriorityEntry[]> & { basis: string };
};

export const allowance = () => api<Allowance>("/what-if/allowance");
export const job = (id: number) => api<Job>(`/jobs/${id}`);
export const simulation = (id: number) => api<Simulation>(`/simulations/${id}`);
export const results = (id: number) => api<{ summary: Summary; results: ResultRow[] }>(`/simulations/${id}/results`);
export const trace = (id: number) => api<Trace>(`/simulations/${id}/trace`);
export const listForLot = (lotId: number | string) => api<{ simulations: ListedSimulation[] }>(`/lots/${lotId}/simulations`);
export const discard = (id: number) => post<Simulation>(`/simulations/${id}/discard`, {});
export const run = (id: number) => post<Simulation>(`/simulations/${id}/run`, {});

/** A scenario and its whole hypothesis in one request: if any change is invalid, nothing is created. */
export const create = (assessmentId: number, body: { title?: string; as_of?: string; changes: Change[] }) =>
  post<Simulation>("/simulations", { assessment_id: assessmentId, ...body });
/** A new scenario on exactly the parent's frozen snapshot. The parent is not changed. */
export const createVariation = (parentId: number, body: { title?: string; changes: Change[] }) =>
  post<Simulation>(`/simulations/${parentId}/variations`, body);
export const inputsForAssessment = (lotId: number | string, assessmentId: number) =>
  api<Inputs>(`/lots/${lotId}/simulation-inputs?assessment_id=${assessmentId}`);
export const inputsForSimulation = (id: number) => api<Inputs>(`/simulations/${id}/inputs`);
export const compare = (lotId: number | string, ids: number[]) =>
  api<Comparison>(`/lots/${lotId}/simulations/compare?ids=${ids.join(",")}`);
export const MAX_COMPARED = 4;

export async function ask(assessmentId: number, question: string, asOf?: string) {
  return post<{ simulation_id: number; job_id: number }>(`/assessments/${assessmentId}/what-if`,
    { question, ...(asOf ? { as_of: asOf } : {}) });
}

/** Why a request did not succeed, in terms the page can act on. */
export type Refusal =
  | { kind: "active"; message: string; simulationId?: number; jobId?: number }
  | { kind: "allowance"; message: string; remaining?: number; nextAt?: string }
  | { kind: "busy"; message: string; retryAfter: number }
  | { kind: "invalid"; message: string }
  | { kind: "not_found"; message: string }
  | { kind: "signed_out"; message: string }
  | { kind: "forbidden"; message: string }
  | { kind: "network"; message: string }
  | { kind: "server"; message: string };

export function refusal(e: unknown): Refusal {
  if (!(e instanceof ApiError)) {
    // fetch rejects with a TypeError when the server cannot be reached at all.
    return { kind: "network", message: "TenderFit couldn't reach its server. Check your connection and try again." };
  }
  const info = e.info ?? {};
  switch (e.status) {
    case 409:
      return { kind: "active", message: e.message, simulationId: num(info.simulation_id), jobId: num(info.job_id) };
    case 429:
      return "next_question_at" in info || "remaining_usd" in info
        ? { kind: "allowance", message: e.message, remaining: num(info.remaining_usd), nextAt: str(info.next_question_at) }
        : { kind: "busy", message: e.message, retryAfter: e.retryAfter ?? 30 };
    case 422: return { kind: "invalid", message: e.message };
    case 404: return { kind: "not_found", message: e.message };
    case 401: return { kind: "signed_out", message: "Your session has ended. Sign in again to continue." };
    case 403: return { kind: "forbidden", message: e.message };
    default:
      return { kind: "server", message: e.status >= 500 ? "TenderFit hit a problem on its side. Nothing was changed; please try again." : e.message };
  }
}

const num = (v: unknown) => (typeof v === "number" ? v : undefined);
const str = (v: unknown) => (typeof v === "string" ? v : undefined);

export const provenance = (id: number, requirementId: number) =>
  api<ResolvedProvenance>(`/simulations/${id}/results/${requirementId}/provenance`);
