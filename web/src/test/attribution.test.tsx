import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { Counterfactuals } from "../whatif/Counterfactuals";
import type { Attribution } from "../whatif/api";

afterEach(cleanup);

it("shows overlapping sufficient and redundant results without choosing a cause", () => {
  const analysis: Attribution = { basis: "Checked alternatives, not a prediction.", changes: [{ ordinal: 2, state: "computed", labels: ["sufficient_alone", "redundant"],
    alone: { state: "decided", ordinals: [2], status: "met", reason: "Enough cover" },
    without: { state: "decided", ordinals: [1], status: "met", reason: "Other policy covers it" } }] };
  render(<Counterfactuals analysis={analysis} />);
  expect(screen.getByText("Which changes matter?")).toBeTruthy();
  expect(screen.getByText(/This change alone reproduces/)).toBeTruthy();
  expect(screen.getByText(/other changes reproduce/)).toBeTruthy();
  expect(screen.getByText("Change 2 alone")).toBeTruthy();
  expect(screen.getByText("All changes except change 2")).toBeTruthy();
  expect(screen.getAllByText("Hypothetical: met")).toHaveLength(2);
});

it("makes budget limits and missing prerequisites explicit without a verdict", () => {
  render(<Counterfactuals analysis={{ basis: "Rules only", changes: [
    { ordinal: 1, state: "limit", labels: [], detail: "Not computed: the extra rule-evaluation limit was reached." },
    { ordinal: 2, state: "undetermined", labels: [], alone: { state: "invalid_subset", ordinals: [2], detail: "fact -1 is missing" },
      without: { state: "needs_model", ordinals: [1] } },
  ] }} />);
  expect(screen.getByText(/rule-evaluation limit/)).toBeTruthy();
  expect(screen.getByText(/Independent effect undetermined/)).toBeTruthy();
  expect(screen.getByText(/omitted change/)).toBeTruthy();
  expect(screen.getByText(/No model was called/)).toBeTruthy();
  expect(screen.queryByText(/Hypothetical:/)).toBeNull();
});

it("renders old results without inventing a calculation", () => {
  render(<Counterfactuals />);
  expect(screen.getByText(/not recorded for this older result/)).toBeTruthy();
});
