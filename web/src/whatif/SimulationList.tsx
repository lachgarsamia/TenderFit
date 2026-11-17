import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { STATIC, fmtDate } from "../api";
import { Empty, ErrorNote, Skeleton, Tag } from "../components/ui";
import * as sims from "./api";
import type { ListedSimulation } from "./api";

const STATUS: Record<string, [string, string]> = {
  created: ["Not run yet", "muted"], running: ["Running", "info"], completed: ["Completed", "ok"],
  failed: ["Did not finish", "warn"], discarded: ["Discarded", "muted"],
};

/** This lot's what-if simulations, newest first. Discarded ones are hidden, as the server does by default. */
export function SimulationList({ lotId }: { lotId: string }) {
  const [rows, setRows] = useState<ListedSimulation[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    if (STATIC) { setRows([]); return; }
    sims.listForLot(lotId).then((r) => { if (!cancelled) setRows(r.simulations); })
      .catch((e) => { if (!cancelled) setError(sims.refusal(e).message); });
    return () => { cancelled = true; };
  }, [lotId]);

  if (error) return <div style={{ padding: "12px 20px" }}><ErrorNote error={error} /></div>;
  if (!rows) return <div style={{ padding: 20 }}><Skeleton lines={3} /></div>;
  if (!rows.length) return <Empty title="No what-if questions yet">{STATIC ? "Questions are switched off in the read-only demo." : "Ask one above. Each answer is a hypothetical simulation; this assessment stays as it is."}</Empty>;
  return <ul className="plain-list sim-list">
    {rows.map((s) => <li key={s.id}>
      <div style={{ minWidth: 0 }}>
        <Link to={`/lots/${lotId}/simulations/${s.id}`}>{s.title ?? `Simulation ${s.id}`}</Link>
        <div className="small muted">{fmtDate(s.created_at)} · {s.changes} hypothetical change{s.changes === 1 ? "" : "s"} · by {s.created_by}</div>
      </div>
      <Tag tone={STATUS[s.status]?.[1] ?? "muted"}>{STATUS[s.status]?.[0] ?? s.status}</Tag>
    </li>)}
  </ul>;
}
