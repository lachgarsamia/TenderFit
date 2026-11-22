import { Tag } from "../components/ui";
import { Evidence } from "./Provenance";
import type { Attribution, AttributionEntry, AttributionProbe } from "./api";

const LABEL: Record<AttributionEntry["labels"][number], string> = {
  sufficient_alone: "This change alone reproduces the full scenario's verdict.",
  necessary: "Removing this change gives a different verdict.",
  jointly_required: "This change needs other changes to reproduce that verdict in these checks.",
  redundant: "The other changes reproduce that verdict without this one.",
  no_independent_effect: "Alone it leaves the control verdict unchanged; removing it leaves the full verdict unchanged.",
};

export function Counterfactuals({ analysis }: { analysis?: Attribution | null }) {
  if (!analysis) return <p className="small muted">Independent alternatives were not recorded for this older result.</p>;
  return <details className="more small"><summary>Which changes matter?</summary>
    <p>{analysis.basis}</p>
    {analysis.changes.map((entry) => <section key={entry.ordinal}>
      <strong>Change {entry.ordinal}</strong>
      {entry.detail && <p>{entry.detail}</p>}
      {entry.state === "undetermined" && <p className="warn-text">Independent effect undetermined: at least one alternative could not be decided.</p>}
      {entry.labels.map((label) => <p key={label}>{LABEL[label]}</p>)}
      {entry.alone && <Probe title={`Change ${entry.ordinal} alone`} probe={entry.alone} />}
      {entry.without && <Probe title={`All changes except change ${entry.ordinal}`} probe={entry.without} />}
    </section>)}
  </details>;
}

function Probe({ title, probe }: { title: string; probe: AttributionProbe }) {
  return <details className="more"><summary>{title}</summary>
    {probe.state === "decided" && <>
      <Tag tone="info">Hypothetical: {probe.status?.replace(/_/g, " ")}</Tag>
      <p>{probe.reason}</p>
      {probe.provenance && <Evidence record={probe.provenance} data={null} />}
    </>}
    {probe.state === "needs_model" && <p>The rules cannot decide this alternative. No model was called and nothing was guessed.</p>}
    {probe.state === "invalid_subset" && <p>This alternative depends on an omitted change or is invalid in isolation. {probe.detail}</p>}
  </details>;
}
