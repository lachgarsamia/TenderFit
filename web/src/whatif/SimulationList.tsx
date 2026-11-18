import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { STATIC, fmtDate } from "../api";
import { Empty, ErrorNote, Skeleton, Tag } from "../components/ui";
import * as sims from "./api";
import type { ListedSimulation } from "./api";

const STATUS: Record<string, [string, string]> = {
  created: ["Not run yet", "muted"], running: ["Running", "info"], completed: ["Completed", "ok"],
  failed: ["Did not finish", "warn"], discarded: ["Discarded", "muted"],
};

/** This lot's what-if simulations, newest first. Discarded ones are hidden, as the server does by default. Completed
 * scenarios of this assessment can be ticked and compared. */
export function SimulationList({ lotId, assessmentId }: { lotId: string; assessmentId?: number }) {
  const navigate = useNavigate();
  const [rows, setRows] = useState<ListedSimulation[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [picked, setPicked] = useState<number[]>([]);
  useEffect(() => {
    let cancelled = false;
    if (STATIC) { setRows([]); return; }
    sims.listForLot(lotId).then((r) => { if (!cancelled) setRows(r.simulations); })
      .catch((e) => { if (!cancelled) setError(sims.refusal(e).message); });
    return () => { cancelled = true; };
  }, [lotId]);

  if (error) return <div style={{ padding: "12px 20px" }}><ErrorNote error={error} /></div>;
  if (!rows) return <div style={{ padding: 20 }}><Skeleton lines={3} /></div>;
  const comparable = (s: ListedSimulation) => s.status === "completed" && (assessmentId === undefined || s.baseline_assessment_id === assessmentId);
  const toggle = (id: number) => setPicked(picked.includes(id) ? picked.filter((x) => x !== id) : [...picked, id]);
  const tooMany = picked.length > sims.MAX_COMPARED;
  const build = !STATIC && assessmentId !== undefined &&
    <Link className="btn" to={`/lots/${lotId}/scenarios/new?assessment=${assessmentId}`}>Build a scenario</Link>;

  if (!rows.length) return <Empty title="No what-if questions yet">
    {STATIC ? "Questions are switched off in the read-only demo." : "Ask one above, or build a scenario by hand. Each is a hypothetical simulation; this assessment stays as it is."}
    {build && <div style={{ marginTop: 10 }}>{build}</div>}
  </Empty>;
  const byId = new Map(rows.map((s) => [s.id, s]));
  return <>
    <div className="sim-toolbar">
      <span className="small muted">{picked.length ? `${picked.length} chosen to compare` : "Tick 2 to 4 completed scenarios to compare them."}</span>
      <div className="actions">
        {build}
        <button className="btn primary" disabled={picked.length < 2 || tooMany}
          onClick={() => navigate(`/lots/${lotId}/compare?ids=${picked.join(",")}`)}>Compare{picked.length >= 2 ? ` ${picked.length}` : ""}</button>
      </div>
    </div>
    {tooMany && <p className="small warn-text" style={{ padding: "0 20px" }}>Compare at most {sims.MAX_COMPARED} at a time.</p>}
    <ul className="plain-list sim-list">
      {rows.map((s) => {
        const can = comparable(s);
        const name = s.title ?? `Simulation ${s.id}`;
        return <li key={s.id}>
          <input type="checkbox" className="sim-pick" checked={picked.includes(s.id)} disabled={!can} onChange={() => toggle(s.id)}
            aria-label={`Compare ${name}`} title={can ? undefined : s.status === "completed" ? "From an earlier assessment" : "Only completed scenarios can be compared"} />
          <div style={{ minWidth: 0, flex: 1 }}>
            <Link to={`/lots/${lotId}/simulations/${s.id}`}>{name}</Link>
            <div className="small muted">{fmtDate(s.created_at)} · {s.changes} hypothetical change{s.changes === 1 ? "" : "s"} · by {s.created_by}
              {s.parent_id && <> · variation of {byId.get(s.parent_id)?.title ?? `scenario ${s.parent_id}`}</>}</div>
          </div>
          <Tag tone={STATUS[s.status]?.[1] ?? "muted"}>{STATUS[s.status]?.[0] ?? s.status}</Tag>
        </li>;
      })}
    </ul>
  </>;
}
