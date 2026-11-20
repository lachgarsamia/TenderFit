import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { EvidenceBadge, verifiedSupport } from "../components/EvidenceBadge";
import { Provenance } from "../whatif/Provenance";
import type { Provenance as Record, ResultRow } from "../whatif/api";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("never labels a hypothetical or modified document as verified evidence", () => {
  const { rerender } = render(<EvidenceBadge status="document_on_file" verified origin="hypothetical" />);
  expect(screen.getByText("Assumption (this scenario)")).toBeTruthy();
  expect(screen.queryByText("Verified evidence")).toBeNull();
  rerender(<EvidenceBadge status="document_on_file" verified origin="modified" />);
  expect(screen.queryByText("Verified evidence")).toBeNull();
  rerender(<EvidenceBadge status="document_on_file" verified />);
  expect(screen.getByText("Verified evidence")).toBeTruthy();
  rerender(<EvidenceBadge status="document_on_file" />);
  expect(screen.getByText("Document on file, unchecked")).toBeTruthy();
  rerender(<EvidenceBadge status="declared" />);
  expect(screen.getByText("Declared")).toBeTruthy();
  rerender(<EvidenceBadge status="unknown" />);
  expect(screen.getByText("Missing / unknown")).toBeTruthy();
});

const record: Record = {
  citations: [{ passage_id: 8, quote: "Insurance must cover £5m" }], support_available: true, needs_human: "G3", gaps: [], params: [],
  requirement_assumptions: [{ ordinal: 2, change: { kind: "set_condition_bound", requirement_id: 1, bound: 3 } }],
  facts: [{ id: 2, label: "Liability cover", origin: "modified", value: { amount_gbp: 5000000 }, evidence_status: "document_on_file",
    valid_until: null, declaration_confirmed: false, changes: [{ ordinal: 1, kind: "set_fact_value", before: { value: { amount_gbp: 1000000 } }, after: { value: { amount_gbp: 5000000 } } }],
    support: [{ evidence_file_id: 3, page: 1, quote: "Cover £1m", verified: true, human_verified: false }] }],
};
const row = { requirement_id: 1, simulated_status: "met", provenance: record } as ResultRow;

it("shows original quotes, modified facts and assumptions together, resolving sources only on demand", async () => {
  const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ passages: { 8: { filename: "Tender.pdf", text: "Insurance must cover £5m", section: "Insurance" } }, evidence: { 3: { filename: "Policy.pdf" } } })));
  vi.stubGlobal("fetch", fetcher);
  const { container } = render(<Provenance row={row} simulationId={21} />);
  expect(fetcher).not.toHaveBeenCalled();
  const details = container.querySelector("details")!;
  details.open = true; fireEvent(details, new Event("toggle"));
  await waitFor(() => expect(screen.getByText("Policy.pdf · page 1")).toBeTruthy());
  expect(screen.getByText("Rule conclusion")).toBeTruthy();
  expect(screen.getByText("Needs a person")).toBeTruthy();
  expect(screen.getByText("Modified")).toBeTruthy();
  expect(screen.getByText("Cover £1m")).toBeTruthy();
  expect(screen.getByText(/These documents support the original fact/)).toBeTruthy();
  expect(screen.queryByText("Verified evidence")).toBeNull();
  expect(fetcher.mock.calls[0][0]).toContain("/simulations/21/results/1/provenance");
});

it("keeps old results readable without inventing provenance", () => {
  render(<Provenance row={{ ...row, provenance: null }} simulationId={21} />);
  expect(screen.getByText(/Evidence provenance was not saved/)).toBeTruthy();
});

it("keeps frozen evidence visible when source resolution fails", async () => {
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
  const { container } = render(<Provenance row={row} simulationId={21} />);
  const details = container.querySelector("details")!;
  details.open = true; fireEvent(details, new Event("toggle"));
  await waitFor(() => expect(screen.getByText(/source details could not be loaded/)).toBeTruthy());
  expect(screen.getByText("Cover £1m")).toBeTruthy();
  expect(screen.getByRole("button", { name: "Retry source details" })).toBeTruthy();
});


it("distinguishes a matching quote from evidence that supports the fact", () => {
  expect(verifiedSupport({ verified: true, verification_level: "relevant" })).toBe(false);
  expect(verifiedSupport({ verified: true })).toBe(false);
  expect(verifiedSupport({ verified: true, verification_level: "supports" })).toBe(true);
  expect(verifiedSupport({ verified: false, verification_level: "current" })).toBe(false);
  expect(verifiedSupport({ human_verified: true })).toBe(true);
});
