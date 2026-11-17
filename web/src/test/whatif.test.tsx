/** Mode B in the browser: asking a what-if question, following it, and reading its hypothetical result.
 * Fixtures follow the server's real responses (captured from the API on the test database). */
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import { ApiError } from "../api";
import { ErrorBoundary } from "../components/ErrorBoundary";
import { refusal } from "../whatif/api";
import { POLL_MS } from "../whatif/useInvestigation";

const ME = { email: "a@test.example", profiles: [{ id: 7, name: "Example Supplier", regions: [], description: "", is_fictional: true }], active_profile_id: 7 };
type Reply = { status?: number; body?: unknown; headers?: Record<string, string> };
type Calls = { method: string; path: string; body?: any }[];

function server(routes: Record<string, Reply | (() => Reply)>): Calls {
  const calls: Calls = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const path = url.replace("/api/v1", "").split("?")[0];
    calls.push({ method, path, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    const found = routes[`${method} ${path}`];
    if (found === undefined && path === "/__offline__") throw new TypeError("Failed to fetch");
    const r = typeof found === "function" ? found() : found ?? { status: 404, body: { detail: `no route ${method} ${path}` } };
    return new Response(JSON.stringify(r.body ?? {}), { status: r.status ?? 200, headers: { "content-type": "application/json", ...r.headers } });
  }));
  return calls;
}

const REC_BEFORE = "Blocker found: Deliver all desks within 30 days of order";
const REC_AFTER = "Provisional: no blocker found in documents read (1 of 3)";
const ASSESSMENT = {
  assessment_id: 11, lot: { id: 1 }, version: { version_no: 1, versions: 1 }, status: { status: "open", last_checked_at: null }, on_current_version: true,
  processing: null, last_failure: null, rejections: [], recommendation: REC_BEFORE,
  dimensions: { relevance: { level: "strong", detail: "" }, eligibility: { state: "blocker_found", label: "Blocker found", detail: "" }, evidence: { gaps: 0, detail: "" }, feasibility: { level: "blocker", detail: "" }, open_questions: { count: 0, items: [] } },
  requirements: [{ id: 1, statement: "Deliver all desks within 30 days of order", status: "not_met", decided_by: "rule", obligation: "mandatory", document: "Spec.pdf", section: "3.2", page: 6, quote: "within 30 days", reason: "lead time 45 days exceeds the required 30", facts: [], citations: [], retrieval: [], conditions: [] }],
};
const LOT = { id: 1, lot_no: "1", title: "Desks", opportunity_id: 3, opportunity: "Furniture", buyer: "Buyer", deadline: null, lots: [] };
const ALLOWANCE = { daily_usd: 5, session_usd: 0.5, spent_24h_usd: 0.4, remaining_usd: 4.6, can_ask: true, next_question_at: null, active: null };
const QUESTION = "What if the buyer accepted our 45-day lead time?";

const SIM = {
  hypothetical: true, notice: "Hypothetical", id: 21, lot_id: 1, status: "created", title: QUESTION,
  baseline: { assessment_id: 11, version_id: 1, recommendation: REC_BEFORE, superseded: false }, as_of: "2026-10-14",
  drift: { baseline_superseded: false, baseline_on_current_version: true, facts_changed_since_baseline: [] }, changes: [], summary: null, failure: null,
  created_by: "a@test.example", created_at: "2026-11-17T16:51:00Z", started_at: null, finished_at: null, discarded_at: null,
};
const CHANGED = [{ requirement_id: 1, statement: "Deliver all desks within 30 days of order", obligation: "mandatory", from: "not_met", to: "needs_confirmation" }];
const SUMMARY = {
  requirements: 3, changed: CHANGED, needs_model: [], not_affected: [2, 3], drift: [], stale_baseline: [],
  input_drift: { since_creation: [], since_baseline: [] },
  recommendation: { baseline: REC_BEFORE, control: REC_BEFORE, simulated: REC_AFTER, complete: true, drift: false },
  execution: { frozen: ["supplier facts", "requirement wording and conditions", "tender fields", "as_of date"], live_inputs: { overrides: { changed: false, at_creation: "x" }, documents: { changed: false, at_creation: "x" } } },
};
const DONE = { ...SIM, status: "completed", changes: [{ kind: "set_condition_bound", requirement_id: 1, condition: 0, bound: 45 }], summary: SUMMARY };
const RESULTS = [
  { requirement_id: 1, statement: "Deliver all desks within 30 days of order", obligation: "mandatory", baseline_status: "not_met", control_status: "not_met", simulated_status: "needs_confirmation", how: "rule", effect: true, drift: false,
    reached: [{ kind: "direct", target: "requirement:1", detail: "the hypothesis changes this requirement's own conditions or wording" }],
    reason_control: "lead time 45 days exceeds the required 30", reason_simulated: "lead time 45 days is within the required 45 (declared)", notes: [], input_drift: [] },
  { requirement_id: 2, statement: "Supply 200 student desks", obligation: "mandatory", baseline_status: "met", control_status: "met", simulated_status: "met", how: "not_affected", effect: false, drift: false, reached: [],
    reason_control: "capacity 300 covers 200", reason_simulated: "capacity 300 covers 200", notes: [], input_drift: [] },
  { requirement_id: 3, statement: "Installation included", obligation: "mandatory", baseline_status: "met", control_status: null, simulated_status: null, how: "not_affected", effect: false, drift: false, reached: [],
    reason_control: null, reason_simulated: null, notes: [], input_drift: [] },
];
const step = (n: number, tool: string, input: object = {}, output: object = {}, cost = 0.008, estimated = false) =>
  ({ step: n, tool, input, output, model: tool === "question" ? null : "claude-opus-5-5", tokens: { input: 1000, output: 200 }, cost_usd: tool === "question" ? 0 : cost, cost_estimated: estimated, created_at: "2026-11-17T16:51:01Z" });
const STEPS = [step(1, "question", { question: QUESTION }), step(2, "read_baseline", {}, { requirements: [1, 2, 3] }),
  step(3, "add_change", { change: JSON.stringify({ kind: "set_condition_bound", requirement_id: 1, condition: 0, bound: 45 }) }, { recorded: true, ordinal: 1 }),
  step(4, "preview", {}, { reached: [{ requirement_id: 1 }], not_reached: [2, 3] }), step(5, "run_simulation", {}, { summary: { changed: CHANGED } }),
  step(6, "finish", {}, { outcome: "answered" })];
const ANSWERED = { outcome: "answered", simulation_id: 21, cost_usd: 0.04, tool_calls: 5, answer: "Lead time would no longer be a blocker, but it must be confirmed.",
  changed: CHANGED, caveats: [], recommendation: { before: REC_BEFORE, after: REC_AFTER, complete: true } };
const job = (status: string, extra: object = {}) => ({ id: 31, kind: "simulation_agent", status, attempts: status === "queued" ? 0 : 1, last_error: null, created_at: "", finished_at: null, waiting_behind: 0, ...extra });
const trace = (jobStatus: string | null, steps: object[], outcome: object | null = null) =>
  ({ hypothetical: true, simulation_id: 21, job: jobStatus ? { id: 31, status: jobStatus } : null, outcome, steps });

const BASE = { "GET /auth/me": { body: ME }, "GET /changes": { body: [] }, "GET /lots/1": { body: LOT }, "GET /lots/1/documents": { body: { documents: [] } },
  "GET /lots/1/assessment": { body: ASSESSMENT }, "GET /what-if/allowance": { body: ALLOWANCE } };
const COMPLETED = { ...BASE, "GET /simulations/21": { body: DONE }, "GET /simulations/21/trace": { body: trace("done", STEPS, ANSWERED) },
  "GET /jobs/31": { body: job("done") }, "GET /simulations/21/results": { body: { summary: SUMMARY, results: RESULTS } } };

const open = (path: string) => render(<MemoryRouter initialEntries={[path]}><ErrorBoundary><App /></ErrorBoundary></MemoryRouter>);
const mutations = (calls: Calls) => calls.filter((c) => c.method !== "GET" && !c.path.startsWith("/auth/"));

afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.useRealTimers(); });

async function ask(text = QUESTION) {
  await userEvent.type(await screen.findByLabelText("Your question"), text);
  await userEvent.click(screen.getByRole("button", { name: "Ask" }));
}

describe("asking a what-if question", () => {
  it("submits once, then opens the investigation, which is queued", async () => {
    let release!: () => void;
    const gate = new Promise<void>((r) => { release = r; });
    const calls = server({ ...BASE,
      "POST /assessments/11/what-if": { status: 202, body: { hypothetical: true, simulation_id: 21, job_id: 31 } },
      "GET /simulations/21": { body: SIM }, "GET /simulations/21/trace": { body: trace("queued", [STEPS[0]]) },
      "GET /jobs/31": { body: job("queued", { waiting_behind: 2 }) } });
    const realFetch = globalThis.fetch as any;
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") await gate;
      return realFetch(url, init);
    }));
    open("/lots/1");
    await userEvent.type(await screen.findByLabelText("Your question"), QUESTION);
    const button = screen.getByRole("button", { name: "Ask" });
    await userEvent.click(button);
    expect(screen.getByRole("button", { name: /Sending/ })).toHaveProperty("disabled", true);
    await userEvent.click(screen.getByRole("button", { name: /Sending/ }));
    release();
    expect(await screen.findByText("Queued")).toBeTruthy();
    expect(screen.getByText(/Waiting behind 2 tasks/)).toBeTruthy();
    const posts = mutations(calls);
    expect(posts).toEqual([{ method: "POST", path: "/assessments/11/what-if", body: { question: QUESTION } }]);
    expect(JSON.stringify(posts)).not.toContain("profile");
  });

  it("shows the daily allowance without implying it is free", async () => {
    server(BASE);
    open("/lots/1");
    expect(await screen.findByText(/\$4\.60 of \$5\.00 left in the last 24 hours/)).toBeTruthy();
    expect(screen.getByText(/One question can use up to \$0\.50/)).toBeTruthy();
  });

  it("an investigation already running: says so and links to it", async () => {
    server({ ...BASE, "POST /assessments/11/what-if": { status: 409, body: { detail: { message: "you already have a what-if question queued or running; wait for it to finish", simulation_id: 19, job_id: 29 } } } });
    open("/lots/1");
    await ask();
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("already have an investigation queued or running");
    expect(within(alert).getByRole("link", { name: "Open it" }).getAttribute("href")).toBe("/lots/1/simulations/19");
  });

  it("an active investigation from the allowance disables asking", async () => {
    server({ ...BASE, "GET /what-if/allowance": { body: { ...ALLOWANCE, can_ask: false, active: { simulation_id: 19, job_id: 29 } } } });
    open("/lots/1");
    expect(await screen.findByRole("link", { name: "Open the investigation in progress" })).toBeTruthy();
    expect(screen.getByLabelText("Your question")).toHaveProperty("disabled", true);
  });

  it("daily allowance used up: the amount left and when it frees up", async () => {
    server({ ...BASE, "POST /assessments/11/what-if": { status: 429, headers: { "Retry-After": "3600" },
      body: { detail: { message: "today's what-if allowance cannot cover another question yet", remaining_usd: 0.4, next_question_at: "2026-11-18T09:30:00+00:00" } } } });
    open("/lots/1");
    await ask();
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("($0.40 left)");
    expect(alert.textContent).toContain("You can ask again from 18 Nov 2026");
    expect(within(alert).queryByRole("button", { name: "Try again" })).toBeNull();
  });

  it("queue full: explains it is busy and lets the user retry", async () => {
    let n = 0;
    const calls = server({ ...BASE, "GET /simulations/21": { body: SIM }, "GET /simulations/21/trace": { body: trace("queued", [STEPS[0]]) }, "GET /jobs/31": { body: job("queued") },
      "POST /assessments/11/what-if": () => (++n === 1
        ? { status: 429, headers: { "Retry-After": "30" }, body: { detail: { message: "too many what-if questions are waiting; try again shortly" } } }
        : { status: 202, body: { simulation_id: 21, job_id: 31 } }) });
    open("/lots/1");
    await ask();
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("busy with other questions");
    expect(alert.textContent).toContain("about 30 seconds");
    await userEvent.click(within(alert).getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Queued")).toBeTruthy();
    expect(mutations(calls).length).toBe(2);
  });

  it("an invalid question and a server failure are both shown, never swallowed", async () => {
    server({ ...BASE, "POST /assessments/11/what-if": { status: 422, body: { detail: "ask a question of 1 to 2000 characters" } } });
    open("/lots/1");
    await ask("   x");
    expect((await screen.findByRole("alert")).textContent).toContain("ask a question of 1 to 2000 characters");
    cleanup();
    server({ ...BASE, "POST /assessments/11/what-if": { status: 500, body: { detail: "TenderFit hit an unexpected error." } } });
    open("/lots/1");
    await ask();
    expect((await screen.findByRole("alert")).textContent).toContain("problem on its side");
  });

  it("errors are normalized in one place, whatever their shape", () => {
    expect(refusal(new TypeError("Failed to fetch")).kind).toBe("network");
    expect(refusal(new ApiError("sign in to continue", 401)).kind).toBe("signed_out");
    expect(refusal(new ApiError("missing header", 403)).kind).toBe("forbidden");
    expect(refusal(new ApiError("x", 429, { message: "x" }, 12))).toEqual({ kind: "busy", message: "x", retryAfter: 12 });
    expect(refusal(new ApiError("x", 409, { simulation_id: 4, job_id: 5 }))).toMatchObject({ kind: "active", simulationId: 4, jobId: 5 });
  });

  it("a session that ended returns to sign-in", async () => {
    server({ ...BASE, "POST /assessments/11/what-if": { status: 401, body: { detail: "sign in to continue" } }, "GET /auth/config": { body: { registration_enabled: false, demo: false } } });
    open("/lots/1");
    await ask();
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeTruthy();
  });

  it("is not offered while the assessment is being updated", async () => {
    server({ ...BASE, "GET /lots/1/assessment": { body: { ...ASSESSMENT, processing: { status: "running", stage: "assessment", done: 1, total: 3 } } } });
    open("/lots/1");
    expect(await screen.findByText(/You can ask a what-if question once it finishes/)).toBeTruthy();
    expect(screen.queryByLabelText("Your question")).toBeNull();
  });
});

describe("following an investigation", () => {
  it("polls while queued and running, shows steps as they arrive, then stops at the result", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let phase: "queued" | "running" | "done" = "queued";
    const calls = server({ ...BASE,
      "GET /simulations/21": () => ({ body: phase === "done" ? DONE : SIM }),
      "GET /simulations/21/trace": () => ({ body: phase === "queued" ? trace("queued", [STEPS[0]]) : phase === "running" ? trace("running", STEPS.slice(0, 3)) : trace("done", STEPS, ANSWERED) }),
      "GET /jobs/31": () => ({ body: job(phase, phase === "queued" ? { waiting_behind: 1 } : {}) }),
      "GET /simulations/21/results": { body: { summary: SUMMARY, results: RESULTS } } });
    open("/lots/1/simulations/21");
    expect(await screen.findByText(/Waiting behind 1 task\./)).toBeTruthy();

    phase = "running";
    await act(() => vi.advanceTimersByTimeAsync(POLL_MS));
    expect(await screen.findByText("Investigating…")).toBeTruthy();
    expect(screen.getByText("Proposed a hypothetical change")).toBeTruthy();

    phase = "done";
    await act(() => vi.advanceTimersByTimeAsync(POLL_MS));
    expect(await screen.findByText("The answer")).toBeTruthy();
    expect(screen.queryByText("Investigating…")).toBeNull();

    const polled = calls.filter((c) => c.path === "/jobs/31").length;
    await act(() => vi.advanceTimersByTimeAsync(POLL_MS * 6));
    expect(calls.filter((c) => c.path === "/jobs/31").length).toBe(polled);
  });

  it("stops polling when the page is left", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const calls = server({ ...BASE, "GET /simulations/21": { body: SIM }, "GET /simulations/21/trace": { body: trace("queued", [STEPS[0]]) }, "GET /jobs/31": { body: job("queued") } });
    const view = open("/lots/1/simulations/21");
    expect(await screen.findByText("Queued")).toBeTruthy();
    await act(() => vi.advanceTimersByTimeAsync(POLL_MS));
    view.unmount();
    const polled = calls.filter((c) => c.path === "/jobs/31").length;
    await act(() => vi.advanceTimersByTimeAsync(POLL_MS * 5));
    expect(calls.filter((c) => c.path === "/jobs/31").length).toBe(polled);
  });

  it("a worker that died part-way shows as interrupted, with a way to ask again", async () => {
    server({ ...BASE, "GET /simulations/21": { body: SIM }, "GET /simulations/21/trace": { body: trace("failed", STEPS.slice(0, 2)) },
      "GET /jobs/31": { body: job("failed", { last_error: "worker stopped while running this job" }) } });
    open("/lots/1/simulations/21");
    expect(await screen.findByText("Interrupted")).toBeTruthy();
    expect(screen.getByText(/not retried automatically/)).toBeTruthy();
    expect(screen.getByRole("link", { name: "Ask again" }).getAttribute("href")).toBe("/lots/1");
  });

  it("a failed job says so without internals", async () => {
    server({ ...BASE, "GET /simulations/21": { body: SIM }, "GET /simulations/21/trace": { body: trace("failed", STEPS.slice(0, 1)) },
      "GET /jobs/31": { body: job("failed", { last_error: "RuntimeError: bug in the client" }) } });
    open("/lots/1/simulations/21");
    expect(await screen.findByText("The investigation failed")).toBeTruthy();
    expect(screen.queryByText(/RuntimeError/)).toBeNull();
  });

  it.each([
    ["budget", "stopped: the session's time limit was reached before an answer", "The investigation stopped at a limit", /time limit/],
    ["unsupported", "price is not something the requirements test", "This question can't be simulated", /price is not/],
    ["agent_unavailable", "the model could not be reached; nothing was guessed", "The AI service was unavailable", /Nothing was guessed/],
  ])("an investigation that ended %s explains why", async (outcome, reason, title, detail) => {
    server({ ...BASE, "GET /simulations/21": { body: SIM }, "GET /jobs/31": { body: job("done") },
      "GET /simulations/21/trace": { body: trace("done", [STEPS[0], step(2, "end", {}, { outcome, reason })], { outcome, simulation_id: 21, cost_usd: 0, tool_calls: 0, reason }) } });
    open("/lots/1/simulations/21");
    expect(await screen.findByText(title)).toBeTruthy();
    expect(screen.getAllByText(detail).length).toBeGreaterThan(0);
  });

  it("another supplier's or a missing simulation is simply not found", async () => {
    server({ ...BASE, "GET /simulations/99": { status: 404, body: { detail: "simulation not found" } }, "GET /simulations/99/trace": { status: 404, body: { detail: "simulation not found" } } });
    open("/lots/1/simulations/99");
    expect((await screen.findByRole("alert")).textContent).toContain("could not be found for your supplier");
    expect(screen.getByRole("note", { name: "Hypothetical simulation" })).toBeTruthy();
  });
});

describe("the hypothetical result", () => {
  it("leads with the impact, then the verified answer, then the model's prose", async () => {
    server(COMPLETED);
    open("/lots/1/simulations/21");
    expect(await screen.findByText("Impact")).toBeTruthy();
    const order = ["Impact", "The answer", "Requirement by requirement", "How this was worked out"].map((t) => screen.getByText(t));
    for (let i = 1; i < order.length; i++) expect(order[i - 1].compareDocumentPosition(order[i]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getAllByText(REC_AFTER).length).toBeGreaterThan(0);
    const verified = screen.getByText("Verified changes");
    const prose = screen.getByText("Explanation, written by the AI model");
    expect(verified.compareDocumentPosition(prose) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(screen.getByRole("list", { name: "Hypothetical changes" })).getByText(/the limit becomes 45/)).toBeTruthy();
  });

  it("separates changed requirements from unchanged ones, and marks every simulated verdict", async () => {
    server(COMPLETED);
    open("/lots/1/simulations/21");
    const changed = await screen.findByRole("table", { name: "Requirements whose verdict changes" });
    expect(within(changed).getAllByRole("row").length).toBe(2);
    expect(within(changed).getByText(/within the required 45/)).toBeTruthy();
    expect(within(changed).getAllByText("(hypothetical)").length).toBe(2);
    const unchanged = screen.getByRole("table", { name: "Requirements whose verdict does not change", hidden: true });
    expect(within(unchanged).getAllByRole("row", { hidden: true }).length).toBe(3);
    expect(within(unchanged).getByText(/its verdict stands as in your assessment/, { selector: "span" })).toBeTruthy();
  });

  it("shows input drift and verdicts that differ for other reasons", async () => {
    const drifted = { ...SUMMARY, drift: [{ requirement_id: 2, baseline: "met", control: "not_met" }],
      input_drift: { since_creation: [{ input: "overrides", requirement_id: 1, detail: "an override of this requirement was recorded or changed" }],
        since_baseline: [{ input: "answers", requirement_id: 2, detail: "an answer was recorded after the baseline ran" }] },
      recommendation: { ...SUMMARY.recommendation, drift: true, baseline: "No blocker found" } };
    const rows = RESULTS.map((r) => r.requirement_id === 2 ? { ...r, drift: true, control_status: "not_met", simulated_status: "not_met",
      input_drift: [{ input: "answers", requirement_id: 2, detail: "x" }] } : r);
    server({ ...COMPLETED, "GET /simulations/21": { body: { ...DONE, summary: drifted } }, "GET /simulations/21/results": { body: { summary: drifted, results: rows } } });
    open("/lots/1/simulations/21");
    expect(await screen.findByText("Changed since this simulation was made")).toBeTruthy();
    expect(screen.getByText(/an override of this requirement was recorded or changed/)).toBeTruthy();
    expect(screen.getByText("Recorded after your assessment ran")).toBeTruthy();
    expect(screen.getByText(/that difference is not caused by the hypothesis/)).toBeTruthy();
    expect(screen.getAllByText("Differs for other reasons").length).toBeGreaterThan(0);
  });

  it("renders the trace in plain words, marks estimated costs and keeps raw data behind Details", async () => {
    const steps = [...STEPS.slice(0, 2), step(3, "add_change", { change: "{\"kind\": \"remove_fact\", \"fact_id\": 99}" }, { error: "fact 99 is not in this simulation's snapshot" }),
      step(4, "llm_error", { estimated_input_tokens: 3000 }, { error: "provider unreachable: timed out", cost_estimated: true }, 0.094, true)];
    server({ ...COMPLETED, "GET /simulations/21": { body: SIM }, "GET /simulations/21/trace": { body: trace("done", steps,
      { outcome: "agent_unavailable", simulation_id: 21, cost_usd: 0.1, tool_calls: 1, reason: "the model could not be reached" }) } });
    open("/lots/1/simulations/21");
    expect(await screen.findByText("Read the frozen copy of your assessment")).toBeTruthy();
    expect(screen.getByText(/Not accepted: fact 99 is not in this simulation's snapshot/)).toBeTruthy();
    expect(screen.getByText("The AI call failed")).toBeTruthy();
    expect(screen.getByText("$0.09 (estimated)")).toBeTruthy();
    expect(screen.getByText(/partly estimated/)).toBeTruthy();
    expect(screen.getAllByText("Details").length).toBe(steps.length);
  });
});

describe("hypothetical never passes for live", () => {
  it("every simulation page carries the banner, offers no way to make it real, and changes nothing", async () => {
    const calls = server(COMPLETED);
    open("/lots/1/simulations/21");
    await screen.findByText("Impact");
    expect(screen.getByRole("note", { name: "Hypothetical simulation" }).textContent).toContain("Your assessment, your profile and the tender are unchanged");
    const page = screen.getByRole("main");
    expect(within(page).queryByText(/make (this|it) real|apply|promote/i)).toBeNull();
    expect(within(page).queryByRole("link", { name: /profile/i })).toBeNull();
    expect(mutations(calls)).toEqual([]);
  });

  it("returning to the assessment shows the live result, untouched", async () => {
    const calls = server(COMPLETED);
    open("/lots/1/simulations/21");
    await screen.findByText("Impact");
    await userEvent.click(screen.getByRole("link", { name: "← Back to your assessment" }));
    expect(await screen.findByText("Relevance")).toBeTruthy();
    expect(screen.getAllByText(REC_BEFORE).length).toBeGreaterThan(0);
    expect(screen.queryByText(REC_AFTER)).toBeNull();
    expect(screen.queryByRole("note", { name: "Hypothetical simulation" })).toBeNull();
    expect(mutations(calls)).toEqual([]);
  });

  it("the assessment lists its what-ifs in their own tab", async () => {
    server({ ...BASE, "GET /lots/1/simulations": { body: { hypothetical: true, simulations: [{ id: 21, lot_id: 1, baseline_assessment_id: 11, status: "completed", title: QUESTION, as_of: "2026-10-14", created_by: "a@test.example", created_at: "2026-11-17T16:51:00Z", changes: 1 }] } } });
    open("/lots/1");
    await userEvent.click(await screen.findByRole("tab", { name: "What-ifs" }));
    expect((await screen.findByRole("link", { name: QUESTION })).getAttribute("href")).toBe("/lots/1/simulations/21");
    expect(screen.getByText("Completed")).toBeTruthy();
  });

  it("discarding asks first and calls only the discard route", async () => {
    const calls = server({ ...COMPLETED, "POST /simulations/21/discard": { body: { ...DONE, status: "discarded" } } });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    open("/lots/1/simulations/21");
    await userEvent.click(await screen.findByRole("button", { name: "Discard" }));
    await waitFor(() => expect(mutations(calls)).toEqual([{ method: "POST", path: "/simulations/21/discard", body: {} }]));
  });
});
