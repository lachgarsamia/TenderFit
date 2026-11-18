/** Mode B, phase 4 in the browser: building a scenario by hand, varying it, and comparing scenarios. Fixtures follow
 * the server's real responses (captured from the API on the test database). */
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import { ErrorBoundary } from "../components/ErrorBoundary";

const ME = { email: "a@test.example", profiles: [{ id: 7, name: "Example Supplier", regions: [], description: "", is_fictional: true }], active_profile_id: 7 };
type Reply = { status?: number; body?: unknown };
type Calls = { method: string; path: string; query: string; body?: any }[];

function server(routes: Record<string, Reply | (() => Reply)>): Calls {
  const calls: Calls = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const [path, query = ""] = url.replace("/api/v1", "").split("?");
    calls.push({ method, path, query, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    const found = routes[`${method} ${path}`];
    const r = typeof found === "function" ? found() : found ?? { status: 404, body: { detail: `no route ${method} ${path}` } };
    return new Response(JSON.stringify(r.body ?? {}), { status: r.status ?? 200, headers: { "content-type": "application/json" } });
  }));
  return calls;
}
const open = (path: string) => render(<MemoryRouter initialEntries={[path]}><ErrorBoundary><App /></ErrorBoundary></MemoryRouter>);
const mutations = (calls: Calls) => calls.filter((c) => c.method !== "GET" && !c.path.startsWith("/auth/"));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

const LEAD = "Deliver all desks within 30 days of order";
const QTY = "Supply 200 student desks";
const STD = "Desks meet BS EN 1729-2 or an accepted equivalent";
const REC_BEFORE = "Blocker found: Deliver all desks within 30 days of order";
const REC_AFTER = "Provisional: no blocker found in documents read (1 of 3)";
const cond = (extra: object) => ({ ordinal: 0, label: null, mandatory: true, machine_checkable: true, kind: null, fact: null, bound: null, match: null, unit: null, accepts: [], ...extra });
const INPUTS = {
  hypothetical: false, assessment_id: 11, lot_id: 1, as_of: "2026-10-14",
  requirements: [
    { id: 1, statement: LEAD, obligation: "mandatory", category: "delivery", baseline_status: "not_met", quote: "The supplier shall deliver all desks within 30 days of order.", passage_id: 9,
      conditions: [cond({ kind: "at_most", fact: "capacity.lead_days", bound: 30, unit: "days", accepts: ["set_condition_bound"] })] },
    { id: 2, statement: QTY, obligation: "mandatory", category: "quantity", baseline_status: "met", quote: null, passage_id: 7,
      conditions: [cond({ kind: "at_least", fact: "capacity.max_units", bound: "lot.quantity" })] },
    { id: 4, statement: STD, obligation: "mandatory", category: "standard", baseline_status: "unknown", quote: null, passage_id: 8,
      conditions: [cond({ kind: "has_evidence", fact: "test_report", match: "BS EN 1729-2", accepts: ["set_condition_match"] })] },
  ],
  facts: [{ id: 6, attribute: "capacity", label: "Delivery capacity", value: { lead_days: 45, max_units: 300 }, evidence_status: "declared", valid_until: null, editable_keys: ["lead_days", "max_units", "scope", "teams"] }],
  fields: { "notice.deadline": "2026-12-01", "lot.quantity": 200 },
  fact_kinds: { capacity: { keys: { lead_days: "number", max_units: "number", scope: "text", teams: "number" }, required: null },
    test_report: { keys: { standard: "text", product: "text" }, required: "standard" } },
  evidence_statuses: { existing: ["document_on_file", "declared", "missing", "unknown", "needs_update"], added: ["declared", "document_on_file", "missing", "unknown"] },
};
const ASSESSMENT = {
  assessment_id: 11, lot: { id: 1 }, version: { version_no: 1, versions: 1 }, status: { status: "open", last_checked_at: null }, on_current_version: true,
  processing: null, last_failure: null, rejections: [], recommendation: REC_BEFORE,
  dimensions: { relevance: { level: "strong", detail: "" }, eligibility: { state: "blocker_found", label: "Blocker found", detail: "" }, evidence: { gaps: 0, detail: "" }, feasibility: { level: "blocker", detail: "" }, open_questions: { count: 0, items: [] } },
  requirements: [],
};
const LOT = { id: 1, lot_no: "1", title: "Desks", opportunity_id: 3, opportunity: "Furniture", buyer: "Buyer", deadline: null, lots: [] };
const SIM = (id: number, extra: object = {}) => ({
  hypothetical: true, notice: "Hypothetical", id, lot_id: 1, status: "created", title: null, parent_id: null,
  baseline: { assessment_id: 11, version_id: 1, recommendation: REC_BEFORE, superseded: false }, as_of: "2026-10-14", snapshot_sha256: "abc", engine_version: "sim-v1",
  drift: {}, changes: [], summary: null, failure: null, created_by: "a@test.example", created_at: "2026-11-17T16:51:00Z",
  started_at: null, finished_at: null, discarded_at: null, ...extra,
});
const BASE = { "GET /auth/me": { body: ME }, "GET /changes": { body: [] }, "GET /lots/1": { body: LOT }, "GET /lots/1/documents": { body: { documents: [] } },
  "GET /lots/1/assessment": { body: ASSESSMENT }, "GET /what-if/allowance": { body: { daily_usd: 5, session_usd: 0.5, spent_24h_usd: 0, remaining_usd: 5, can_ask: true, next_question_at: null, active: null } },
  "GET /lots/1/simulation-inputs": { body: INPUTS } };
const NO_TRACE = { body: { hypothetical: true, simulation_id: 0, job: null, outcome: null, steps: [] } };

describe("building a scenario by hand", () => {
  it("shows the actual value, lists the change as hypothetical, and creates and runs it in one go", async () => {
    const calls = server({ ...BASE, "POST /simulations": { status: 201, body: SIM(22) }, "POST /simulations/22/run": { body: SIM(22, { status: "completed" }) },
      "GET /simulations/22": { body: SIM(22) }, "GET /simulations/22/trace": NO_TRACE });
    open("/lots/1/scenarios/new?assessment=11");
    expect(await screen.findByRole("heading", { name: "Build a scenario" })).toBeTruthy();
    await userEvent.selectOptions(await screen.findByLabelText("Requirement"), "1:0");
    expect(calls.find((c) => c.path === "/lots/1/simulation-inputs")?.query).toBe("assessment_id=11");
    expect(screen.getByText("Actual").parentElement!.textContent).toContain("30 days");
    await userEvent.type(screen.getByLabelText("Suppose the limit were"), "40");
    await userEvent.click(screen.getByRole("button", { name: "Add to the scenario" }));
    const draft = screen.getByRole("list", { name: "Draft changes" });
    expect(within(draft).getByText("Hypothetical")).toBeTruthy();
    expect(draft.textContent).toContain(`"${LEAD}": the limit becomes 40`);
    expect(mutations(calls)).toEqual([]); // nothing is sent while drafting
    await userEvent.type(screen.getByLabelText("Name (optional)"), "40 days");
    await userEvent.click(screen.getByRole("button", { name: "Create and run" }));
    await waitFor(() => expect(mutations(calls).map((c) => `${c.method} ${c.path}`)).toEqual(["POST /simulations", "POST /simulations/22/run"]));
    expect(mutations(calls)[0].body).toEqual({ assessment_id: 11, title: "40 days", changes: [{ kind: "set_condition_bound", requirement_id: 1, condition: 0, bound: 40 }] });
    expect(JSON.stringify(mutations(calls))).not.toContain("profile");
  });

  it("checks values before sending, and shows the server's refusal naming the change", async () => {
    const calls = server({ ...BASE, "POST /simulations": { status: 422, body: { detail: "change 1 (set_condition_bound): requirement 1 has no condition 3" } } });
    open("/lots/1/scenarios/new?assessment=11");
    await userEvent.selectOptions(await screen.findByLabelText("Requirement"), "1:0");
    await userEvent.type(screen.getByLabelText("Suppose the limit were"), "soon");
    await userEvent.click(screen.getByRole("button", { name: "Add to the scenario" }));
    expect(screen.getByRole("alert").textContent).toContain("Enter a number");
    await userEvent.clear(screen.getByLabelText("Suppose the limit were"));
    await userEvent.type(screen.getByLabelText("Suppose the limit were"), "40");
    await userEvent.click(screen.getByRole("button", { name: "Add to the scenario" }));
    await userEvent.click(screen.getByRole("button", { name: "Create without running" }));
    expect(await screen.findByText(/change 1 \(set_condition_bound\).*Nothing was created/)).toBeTruthy();
    expect(mutations(calls).map((c) => c.path)).toEqual(["/simulations"]);
  });

  it("a fact you don't hold is labelled hypothetical evidence, and changes can be removed before creating", async () => {
    server(BASE);
    open("/lots/1/scenarios/new?assessment=11");
    await userEvent.selectOptions(await screen.findByLabelText("Kind of change"), "add_fact");
    expect(screen.getByRole("note", { name: "Hypothetical evidence" }).textContent).toContain("never added to your profile");
    await userEvent.selectOptions(screen.getByLabelText("Kind of fact"), "test_report");
    await userEvent.type(screen.getByLabelText("Short description"), "BS EN 1729-2 report");
    await userEvent.click(screen.getByRole("button", { name: "Add to the scenario" }));
    expect(screen.getByRole("alert").textContent).toContain("Give the standard");
    await userEvent.type(screen.getByLabelText("standard (required)"), "BS EN 1729-2");
    await userEvent.click(screen.getByRole("button", { name: "Add to the scenario" }));
    const draft = screen.getByRole("list", { name: "Draft changes" });
    expect(draft.textContent).toContain("You don't hold this today");
    await userEvent.click(within(draft).getByRole("button", { name: "Remove change 1" }));
    expect(screen.queryByRole("list", { name: "Draft changes" })).toBeNull();
  });

  it("offers only the targets a change can apply to", async () => {
    server(BASE);
    open("/lots/1/scenarios/new?assessment=11");
    const options = within(await screen.findByLabelText("Requirement")).getAllByRole("option").map((o) => o.textContent);
    expect(options).toEqual(["Choose…", LEAD]); // the quantity's limit comes from the tender; installation has none
    await userEvent.selectOptions(screen.getByLabelText("Kind of change"), "set_condition_match");
    expect(within(screen.getByLabelText("Requirement")).getAllByRole("option").map((o) => o.textContent)).toEqual(["Choose…", STD]);
  });
});

describe("variations from a common baseline", () => {
  const FROM = { ...INPUTS, simulation_id: 21, snapshot_sha256: "abc", changes: [{ kind: "set_condition_bound", requirement_id: 1, condition: 0, bound: 40 }] };
  it("starts from the scenario's changes, which can be edited, and posts a variation of it", async () => {
    const calls = server({ ...BASE, "GET /simulations/21/inputs": { body: FROM },
      "POST /simulations/21/variations": { status: 201, body: SIM(23, { parent_id: 21 }) }, "POST /simulations/23/run": { body: {} },
      "GET /simulations/23": { body: SIM(23, { parent_id: 21 }) }, "GET /simulations/23/trace": NO_TRACE });
    open("/lots/1/scenarios/new?from=21");
    expect(await screen.findByRole("heading", { name: "Try a variation" })).toBeTruthy();
    const draft = screen.getByRole("list", { name: "Draft changes" });
    expect(draft.textContent).toContain("the limit becomes 40");
    await userEvent.click(within(draft).getByRole("button", { name: "Remove change 1" }));
    await userEvent.selectOptions(screen.getByLabelText("Requirement"), "1:0");
    await userEvent.type(screen.getByLabelText("Suppose the limit were"), "20");
    await userEvent.click(screen.getByRole("button", { name: "Add to the scenario" }));
    await userEvent.click(screen.getByRole("button", { name: "Create and run" }));
    await waitFor(() => expect(mutations(calls).map((c) => c.path)).toEqual(["/simulations/21/variations", "/simulations/23/run"]));
    expect(mutations(calls)[0].body).toEqual({ changes: [{ kind: "set_condition_bound", requirement_id: 1, condition: 0, bound: 20 }] });
    expect(await screen.findByRole("link", { name: "scenario 21" })).toBeTruthy();
  });

  it("a scenario not yet run can be run from its page, and offers a variation", async () => {
    const calls = server({ ...BASE, "GET /simulations/22": { body: SIM(22, { changes: FROM.changes }) }, "GET /simulations/22/trace": NO_TRACE,
      "POST /simulations/22/run": { body: {} } });
    open("/lots/1/simulations/22");
    expect(await screen.findByText("Not run yet")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Try a variation" }).getAttribute("href")).toBe("/lots/1/scenarios/new?from=22");
    await userEvent.click(screen.getByRole("button", { name: "Run this scenario" }));
    await waitFor(() => expect(mutations(calls)).toEqual([{ method: "POST", path: "/simulations/22/run", query: "", body: {} }]));
  });
});

const listed = (id: number, extra: object = {}) => ({ id, lot_id: 1, baseline_assessment_id: 11, status: "completed", title: `Scenario ${id}`, as_of: "2026-10-14",
  created_by: "a@test.example", created_at: "2026-11-17T16:51:00Z", changes: 1, parent_id: null, snapshot_sha256: "abc", ...extra });

describe("choosing scenarios to compare", () => {
  it("compares 2 to 4 completed scenarios of this assessment", async () => {
    server({ ...BASE, "GET /lots/1/simulations": { body: { simulations: [listed(31, { title: "45 days" }), listed(32, { title: "20 days", parent_id: 31 }),
      listed(33, { status: "created" }), listed(34, { baseline_assessment_id: 5 })] } }, "GET /lots/1/simulations/compare": { body: COMPARISON } });
    open("/lots/1");
    await userEvent.click(await screen.findByRole("tab", { name: "What-ifs" }));
    expect((await screen.findByRole("link", { name: "Build a scenario" })).getAttribute("href")).toBe("/lots/1/scenarios/new?assessment=11");
    expect(screen.getByText(/variation of 45 days/)).toBeTruthy();
    expect(screen.getByRole("checkbox", { name: "Compare Scenario 33" })).toHaveProperty("disabled", true);
    expect(screen.getByRole("checkbox", { name: "Compare Scenario 34" })).toHaveProperty("disabled", true);
    const compare = screen.getByRole("button", { name: /Compare/ });
    await userEvent.click(screen.getByRole("checkbox", { name: "Compare 45 days" }));
    expect(compare).toHaveProperty("disabled", true);
    await userEvent.click(screen.getByRole("checkbox", { name: "Compare 20 days" }));
    await userEvent.click(screen.getByRole("button", { name: "Compare 2" }));
    expect(await screen.findByRole("heading", { name: "Compare 2 scenarios" })).toBeTruthy();
  });
});

const cell = (control: string | null, simulated: string | null, how: string, direction: string, extra: object = {}) =>
  ({ control, simulated, how, direction, effect: direction !== "unchanged", reason_control: null, reason_simulated: null, reached: [], notes: [], drift: false, input_drift: [], ...extra });
const scen = (id: number, title: string, extra: object) => ({ id, title, parent_id: null, as_of: "2026-10-14", snapshot_sha256: "abc", changes: [],
  recommendation: { baseline: REC_BEFORE, control: REC_BEFORE, simulated: REC_AFTER, complete: true, drift: false },
  counts: { improved: 0, worsened: 0, unchanged: 3, undetermined: 0 }, transitions: {}, no_effect: true,
  highlights: { blockers_resolved: [], blockers_now_unknown: [], blockers_introduced: [], uncertainties_resolved: [], now_needs_confirmation: [], needs_model: [] },
  input_drift: { since_creation: [], since_baseline: [] }, drift: [], ...extra });
const reach = (change: object, extra: object = {}) => ({ ordinal: 1, change, target: "requirement:1", reaches: [1], moved: [1], attributable: [1], shared_with_other_changes: [],
  undetermined: [], blockers_resolved: [1], blockers_introduced: [], uncertainties_resolved: [], no_effect: false, assumes_evidence: false, ...extra });
const B45 = { kind: "set_condition_bound", requirement_id: 1, condition: 0, bound: 45 };
const GONE = { kind: "remove_fact", fact_id: 6 };
const COMPARISON = {
  hypothetical: true, notice: "Hypothetical", lot_id: 1, baseline_assessment_id: 11,
  integrity: { same_snapshot: true, same_live_inputs: true, consistent: true, notes: [] },
  scenarios: [
    scen(31, "45 days", { changes: [reach(B45)], counts: { improved: 1, worsened: 0, unchanged: 2, undetermined: 0 }, transitions: { "not_met -> needs_confirmation": 1 }, no_effect: false,
      highlights: { blockers_resolved: [1], blockers_now_unknown: [], blockers_introduced: [], uncertainties_resolved: [], now_needs_confirmation: [1], needs_model: [] } }),
    scen(32, "No capacity fact", { parent_id: 31, changes: [reach(GONE, { target: "fact:6", reaches: [1, 2], moved: [1, 2], attributable: [1, 2], blockers_resolved: [] })],
      counts: { improved: 1, worsened: 1, unchanged: 1, undetermined: 0 }, transitions: { "not_met -> unknown": 1, "met -> unknown": 1 }, no_effect: false,
      highlights: { blockers_resolved: [], blockers_now_unknown: [1], blockers_introduced: [], uncertainties_resolved: [], now_needs_confirmation: [], needs_model: [] } }),
  ],
  requirements: [
    { requirement_id: 1, statement: LEAD, obligation: "mandatory", category: "delivery", baseline_status: "not_met", quote: "within 30 days of order", passage_id: 9,
      scenarios: { 31: cell("not_met", "needs_confirmation", "rule", "improved", { reason_simulated: "lead time 45 days is within the required 45" }), 32: cell("not_met", "unknown", "rule", "improved") },
      affected: true, differs_between_scenarios: true },
    { requirement_id: 2, statement: QTY, obligation: "mandatory", category: "quantity", baseline_status: "met", quote: null, passage_id: null,
      scenarios: { 31: cell("met", "met", "not_affected", "unchanged"), 32: cell("met", "unknown", "rule", "worsened") }, affected: true, differs_between_scenarios: true },
    { requirement_id: 4, statement: STD, obligation: "mandatory", category: "standard", baseline_status: "unknown", quote: null, passage_id: null,
      scenarios: { 31: cell("unknown", "unknown", "not_affected", "unchanged"), 32: cell("unknown", "unknown", "not_affected", "unchanged") }, affected: false, differs_between_scenarios: false },
  ],
  facts: { 6: "Delivery capacity" },
  order: [31, 32], ordering_rule: "Scenarios are listed by mandatory blockers resolved (most first). This is a sort over the counts shown, not a score.",
  verdict_order: ["not_met", "unknown", "needs_confirmation", "met"],
  priorities: { widest_reach: [{ scenario_id: 32, ordinal: 1, change: GONE, reaches: [1, 2], moved: [1, 2], attributable: [1, 2], blockers_resolved: [], blockers_introduced: [], uncertainties_resolved: [], assumes_evidence: false }],
    addresses_blockers: [{ scenario_id: 31, ordinal: 1, change: B45, reaches: [1], moved: [1], attributable: [1], blockers_resolved: [1], blockers_introduced: [], uncertainties_resolved: [], assumes_evidence: false }],
    resolves_uncertainty: [], introduces_blockers: [], little_or_no_effect: [], depends_on_evidence_not_held: [], basis: "Grouped from the dependency analysis. None of this predicts the outcome of a bid." },
};

describe("comparing scenarios", () => {
  it("keeps every verdict category distinct, names each transition, and never scores", async () => {
    // jsdom has no modal dialogs; a browser does.
    HTMLDialogElement.prototype.showModal = vi.fn(function (this: HTMLDialogElement) { this.setAttribute("open", ""); });
    const calls = server({ ...BASE, "GET /lots/1/simulations/compare": { body: COMPARISON },
      "GET /passages/9": { body: { document: { filename: "Spec.pdf" }, passages: [{ id: 9, text: "The supplier shall deliver all desks within 30 days of order.", section: "3.2", page: 6 }] } } });
    open("/lots/1/compare?ids=31,32");
    expect(await screen.findByText("Compared on identical inputs")).toBeTruthy();
    expect(calls.find((c) => c.path === "/lots/1/simulations/compare")?.query).toBe("ids=31,32");
    expect(screen.getByRole("note", { name: "Hypothetical simulation" })).toBeTruthy();
    const first = screen.getByRole("region", { name: "45 days" });
    expect(within(first).getByText("Mandatory blockers cleared")).toBeTruthy();
    expect(within(first).getAllByText("Needs confirmation").length).toBeGreaterThan(0);
    const second = screen.getByRole("region", { name: "No capacity fact" });
    expect(within(second).getByText("Blockers that become unknown (not cleared)")).toBeTruthy();
    expect(within(second).queryByText("Mandatory blockers cleared")).toBeNull();
    expect(within(second).getByText("Variation of 45 days")).toBeTruthy();
    expect(within(second).getByText(/"Delivery capacity" is taken away/)).toBeTruthy();
    const page = screen.getByRole("main");
    expect(page.textContent!.replace(/not a score/g, "")).not.toMatch(/score/i);
    expect(page.textContent).toContain("not a score");
    // Affected requirements first; the one no scenario affects is collapsed below.
    const tables = within(page).getAllByRole("table");
    expect(within(tables[0]).getByText(LEAD)).toBeTruthy();
    expect(within(tables[0]).queryByText(STD)).toBeNull();
    expect(screen.getByText("Not affected by any scenario · 1")).toBeTruthy();
    expect(screen.getByText("Changes that clear a mandatory blocker")).toBeTruthy();
    expect(screen.getByText(/None of this predicts/)).toBeTruthy();
    await userEvent.click(within(tables[0]).getByRole("button", { name: "Tender source" }));
    expect(await screen.findByRole("heading", { name: "Spec.pdf" })).toBeTruthy();
    expect(mutations(calls)).toEqual([]);
  });

  it("says when the scenarios were not decided on identical inputs", async () => {
    server({ ...BASE, "GET /lots/1/simulations/compare": { body: { ...COMPARISON, integrity: { same_snapshot: false, same_live_inputs: true, consistent: false,
      notes: ["These scenarios were frozen separately, so their supplier facts, requirements or tender fields may differ."] } } } });
    open("/lots/1/compare?ids=31,32");
    expect(await screen.findByText("Read this comparison with care")).toBeTruthy();
    expect(screen.getByText(/frozen separately/)).toBeTruthy();
  });

  it("shows why scenarios cannot be compared", async () => {
    server({ ...BASE, "GET /lots/1/simulations/compare": { status: 409, body: { detail: "these scenarios start from different assessments, so their results cannot be compared" } } });
    open("/lots/1/compare?ids=31,34");
    expect(await screen.findByText(/start from different assessments/)).toBeTruthy();
  });

  it("needs at least two scenarios", async () => {
    const calls = server(BASE);
    open("/lots/1/compare?ids=31");
    expect(await screen.findByText("Choose at least two scenarios to compare.")).toBeTruthy();
    expect(calls.some((c) => c.path.includes("compare"))).toBe(false);
  });
});
