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
  hypothetical: true; notice: string; id: number; lot_id: number; status: SimStatus; title: string | null;
  baseline: { assessment_id: number; version_id: number; recommendation: string | null; superseded: boolean | null };
  as_of: string; drift: { facts_changed_since_baseline?: number[]; baseline_on_current_version?: boolean; baseline_superseded?: boolean; [k: string]: unknown };
  changes: Change[]; summary: Summary | null; failure: { stage: string | null; message: string } | null;
  created_by: string; created_at: string; started_at: string | null; finished_at: string | null; discarded_at: string | null;
};

export type ResultRow = {
  requirement_id: number; statement: string; obligation: string;
  baseline_status: Status | null; control_status: Status | null; simulated_status: Status | null;
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
  as_of: string; created_by: string; created_at: string; changes: number;
};

export const allowance = () => api<Allowance>("/what-if/allowance");
export const job = (id: number) => api<Job>(`/jobs/${id}`);
export const simulation = (id: number) => api<Simulation>(`/simulations/${id}`);
export const results = (id: number) => api<{ summary: Summary; results: ResultRow[] }>(`/simulations/${id}/results`);
export const trace = (id: number) => api<Trace>(`/simulations/${id}/trace`);
export const listForLot = (lotId: number | string) => api<{ simulations: ListedSimulation[] }>(`/lots/${lotId}/simulations`);
export const discard = (id: number) => post<Simulation>(`/simulations/${id}/discard`, {});

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
