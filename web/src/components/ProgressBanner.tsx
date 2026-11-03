const STAGE: Record<string, string> = {
  documents: "Fetching the tender documents",
  embedding: "Indexing the documents",
  extraction: "Reading requirements from the documents",
  assessment: "Checking each requirement",
  finalising: "Finishing",
};

export function ProgressBanner({ p, hasResult }: { p: any; hasResult: boolean }) {
  const stage = p.status === "queued" ? (p.retrying ? "Waiting to try again" : "Waiting to start") : STAGE[p.stage] ?? "Working";
  const pct = p.total ? Math.round(((p.done ?? 0) / p.total) * 100) : null;
  return (
    <div className="banner info" role="status">
      <span className="spinner" aria-hidden />
      <div className="body">
        <strong>Updating assessment</strong>
        <p>{stage}{p.total ? `: ${p.done ?? 0} of ${p.total} requirements` : ""}.{hasResult ? " The result below is the last completed one and stays valid until this finishes." : ""}
          {p.attempt > 1 ? ` Attempt ${p.attempt} of ${p.max_attempts}.` : ""}</p>
        {pct !== null && <div className="progress" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct}><span style={{ width: `${pct}%` }} /></div>}
      </div>
    </div>
  );
}
