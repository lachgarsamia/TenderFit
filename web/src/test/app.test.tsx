import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import { ApiError, api } from "../api";
import { ErrorBoundary } from "../components/ErrorBoundary";
import { ProgressBanner } from "../components/ProgressBanner";

const SUPPLIER = { id: 7, name: "Example Supplier", regions: ["UKE"], description: "", is_fictional: true };
const ME = { email: "a@test.example", profiles: [SUPPLIER], active_profile_id: 7 };

type Route = { status?: number; body?: unknown };
function server(routes: Record<string, Route | (() => Route)>) {
  const calls: { url: string; init?: RequestInit }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    const key = `${init?.method ?? "GET"} ${url.replace("/api/v1", "").split("?")[0]}`;
    const found = routes[key];
    const r = typeof found === "function" ? found() : found ?? { status: 404, body: { detail: `no route ${key}` } };
    return new Response(JSON.stringify(r.body ?? {}), { status: r.status ?? 200, headers: { "content-type": "application/json" } });
  }));
  return calls;
}
const ASSESSMENT = {
  assessment_id: 1, lot: { id: 1 }, version: { version_no: 2, versions: 2 }, status: { status: "open", last_checked_at: null }, on_current_version: true, processing: null, last_failure: null, rejections: [],
  recommendation: "Relevant: 1 item needs confirmation", dimensions: { relevance: { level: "strong", detail: "" }, eligibility: { state: "needs_confirmation", label: "No blocker found", detail: "" }, evidence: { gaps: 0, detail: "" }, feasibility: { level: "unknown", detail: "" }, open_questions: { count: 0, items: [] } },
  requirements: [{ id: 5, statement: "Deliver within 30 days", status: "not_met", decided_by: "rule", obligation: "mandatory", document: "Spec.pdf", section: "3.2 Delivery", page: 3, quote: "deliver within 30 days", reason: "lead time 45 exceeds 30", required: "at most 30 days", observed: "45 days", evidence_quality: "contradictory", facts: [], citations: [{ passage_id: 9, quote: "deliver within 30 days", document: "Spec.pdf", page: 3 }], retrieval: [], conditions: [] }],
};
const LOT = { id: 1, lot_no: "1", title: "Desks", opportunity_id: 3, opportunity: "Furniture", buyer: "Buyer", deadline: null, lots: [{ id: 1, lot_no: "1", title: "Desks" }, { id: 2, lot_no: "2", title: "Chairs" }] };
const SIGNED_IN = { "GET /auth/me": { body: ME }, "GET /changes": { body: [] } };
const LOT_ROUTES = { ...SIGNED_IN, "GET /lots/1": { body: LOT }, "GET /lots/1/documents": { body: { documents: [] } }, "GET /lots/1/assessment": { body: ASSESSMENT } };

function open(path = "/opportunities") {
  return render(<MemoryRouter initialEntries={[path]}><ErrorBoundary><App /></ErrorBoundary></MemoryRouter>);
}

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("sign-in", () => {
  it("shows sign-in when there is no session", async () => {
    server({ "GET /auth/me": { status: 401, body: { detail: "sign in to continue" } }, "GET /auth/config": { body: { registration_enabled: false, demo: false } } });
    open();
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeTruthy();
    expect(await screen.findByText("Accounts are created by the operator.")).toBeTruthy();
  });

  it("shows why a sign-in failed and stays on the form", async () => {
    server({ "GET /auth/me": { status: 401 }, "GET /auth/config": { body: { registration_enabled: true, demo: false } },
      "POST /auth/login": { status: 401, body: { detail: "that email and password do not match" } } });
    open();
    await userEvent.type(await screen.findByLabelText("Email"), "a@test.example");
    await userEvent.type(screen.getByLabelText("Password"), "wrong-password");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveProperty("textContent", "that email and password do not match");
  });

  it("asks a new person to create a supplier profile", async () => {
    server({ "GET /auth/me": { body: { email: "n@test.example", profiles: [], active_profile_id: null } } });
    open();
    expect(await screen.findByRole("heading", { name: "Set up your supplier profile" })).toBeTruthy();
  });

  it("returns to sign-in when the session ends mid-use", async () => {
    server({ "GET /auth/me": { body: ME }, "GET /changes": { body: [] }, "POST /search": { status: 401, body: { detail: "sign in to continue" } },
      "GET /auth/config": { body: { registration_enabled: false, demo: false } } });
    open();
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeTruthy();
  });

  it("explains when the server cannot be reached instead of showing a blank page", async () => {
    server({ "GET /auth/me": { status: 502, body: {} } });
    open();
    expect((await screen.findByRole("alert")).textContent).toContain("can't reach its server");
  });
});

describe("requests", () => {
  it("sends the header the server requires and never a profile id", async () => {
    const calls = server({ "POST /lots/1/assess": { body: { jobs: [] } } });
    await api("/lots/1/assess", { method: "POST", body: "{}" });
    expect((calls[0].init?.headers as Record<string, string>)["X-TenderFit"]).toBe("1");
    expect(calls[0].url).toBe("/api/v1/lots/1/assess");
    expect(calls[0].url).not.toContain("profile_id");
  });

  it("carries the server's message and status on an error", async () => {
    server({ "GET /jobs/1": { status: 404, body: { detail: "job not found" } } });
    const e = await api("/jobs/1").catch((x) => x);
    expect(e).toBeInstanceOf(ApiError);
    expect([e.message, e.status]).toEqual(["job not found", 404]);
  });
});

describe("assessment page", () => {
  it("reaches every lot and shows the verdict's evidence", async () => {
    server(LOT_ROUTES);
    open("/lots/1");
    const nav = await screen.findByRole("navigation", { name: "Lots in this tender" });
    expect(nav.querySelectorAll("a").length).toBe(2);
    expect(nav.querySelector('a[aria-current="page"]')?.textContent).toContain("Desks");
    expect(await screen.findByText("at most 30 days")).toBeTruthy();
    expect(screen.getByText("Fixed rule")).toBeTruthy();
    expect(screen.getByText(/Read in context/)).toBeTruthy();
  });

  it("shows the last result while an update runs", async () => {
    server({ ...LOT_ROUTES, "GET /lots/1/assessment": { body: { ...ASSESSMENT, processing: { status: "running", stage: "assessment", done: 2, total: 5 } } } });
    open("/lots/1");
    expect(await screen.findByText("Updating assessment")).toBeTruthy();
    expect(await screen.findByText("Relevant: 1 item needs confirmation")).toBeTruthy();
  });

  it("never renders dimensions from a run with no result, and offers a retry", async () => {
    server({ ...LOT_ROUTES, "GET /lots/1/assessment": { body: { assessment_id: null, requirements: [], processing: null, lot: {}, last_failure: { error: "provider error 529: overloaded", stage: "extraction" } } } });
    open("/lots/1");
    expect(await screen.findByText("The last update did not finish")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Try again" })).toBeTruthy();
    expect(screen.queryByText("Relevance")).toBeNull();
  });

  it("shows an API failure on the page instead of going blank", async () => {
    server({ ...LOT_ROUTES, "GET /lots/1/assessment": { status: 500, body: { detail: "TenderFit hit an unexpected error." } } });
    open("/lots/1");
    expect((await screen.findByRole("alert")).textContent).toContain("unexpected error");
    expect(screen.getByRole("navigation", { name: "Main" })).toBeTruthy();
  });

  it("names the pages of a partly scanned document", async () => {
    server({ ...LOT_ROUTES, "GET /lots/1/documents": { body: { documents: [{ id: 4, label: "Spec", kind: "document", essential: true, status: "partial", ocr_pages: [2, 3], passages: 1, note: "pages 2-3 of 3 are images and need OCR" }] } } });
    open("/lots/1");
    await waitFor(() => expect(screen.getByText(/pages 2-3 of 3 are images and need OCR/)).toBeTruthy());
    expect(screen.getByText(/could not be read in full/)).toBeTruthy();
  });
});

describe("components", () => {
  it("the error boundary contains a crash and can recover", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    let boom = true;
    const Bomb = () => { if (boom) throw new Error("render failed"); return <div>recovered</div>; };
    render(<ErrorBoundary><Bomb /></ErrorBoundary>);
    expect(screen.getByRole("alert").textContent).toContain("could not be shown");
    boom = false;
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(screen.getByText("recovered")).toBeTruthy();
  });

  it("progress reports the stage and count", () => {
    render(<ProgressBanner p={{ status: "running", stage: "assessment", done: 2, total: 5 }} hasResult />);
    expect(screen.getByRole("status").textContent).toContain("2 of 5 requirements");
    expect(screen.getByRole("progressbar").getAttribute("aria-valuenow")).toBe("40");
  });
});
