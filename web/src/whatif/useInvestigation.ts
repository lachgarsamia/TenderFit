import { useCallback, useEffect, useRef, useState } from "react";
import * as sims from "./api";
import type { Job, ResultRow, Simulation, Trace } from "./api";

export const POLL_MS = 2500;
export const SLOW_POLL_MS = 10_000;
const SLOW_AFTER = 60;        // polls (about 2½ minutes, the session deadline) before slowing down
const GIVE_UP_AFTER = 3;      // consecutive failed polls before stopping and offering a retry

export type Investigation = {
  sim: Simulation | null; trace: Trace | null; job: Job | null; results: ResultRow[] | null;
  loading: boolean; error: string | null; polling: boolean;
  reload: () => void;
};

const active = (j: Job | null | undefined) => j?.status === "queued" || j?.status === "running";

/** Everything the simulation page shows, kept current while its investigation is queued or running.
 *
 * One timer chain per simulation: a new one starts only after the previous one is cancelled (on unmount, on navigating
 * to another simulation, or on reload). Polling stops when the job reaches a terminal state, after which the
 * simulation, its trace and (if it completed) its results are read once more. */
export function useInvestigation(simId: number): Investigation {
  const [state, setState] = useState<Omit<Investigation, "reload">>(
    { sim: null, trace: null, job: null, results: null, loading: true, error: null, polling: false });
  const [generation, setGeneration] = useState(0);
  const reload = useCallback(() => setGeneration((g) => g + 1), []);
  const timer = useRef<ReturnType<typeof setTimeout>>();

  useEffect(() => {
    let cancelled = false;
    let polls = 0;
    let failures = 0;
    const set = (patch: Partial<Investigation>) => { if (!cancelled) setState((s) => ({ ...s, ...patch })); };

    const full = async () => {
      const [sim, trace] = await Promise.all([sims.simulation(simId), sims.trace(simId)]);
      const job = trace.job ? await sims.job(trace.job.id) : null;
      const results = sim.status === "completed" ? (await sims.results(simId)).results : null;
      return { sim, trace, job, results };
    };

    const schedule = (fn: () => void) => {
      if (cancelled) return;
      timer.current = setTimeout(fn, polls >= SLOW_AFTER ? SLOW_POLL_MS : POLL_MS);
    };

    const poll = async (jobId: number) => {
      polls += 1;
      try {
        const job = await sims.job(jobId);
        if (cancelled) return;
        failures = 0;
        if (active(job)) {
          // The trace grows as the agent works; showing it is what "progress" means here.
          const trace = job.status === "running" ? await sims.trace(simId) : undefined;
          set({ job, ...(trace ? { trace } : {}), error: null });
          schedule(() => poll(jobId));
        } else {
          set({ ...(await full()), polling: false, error: null });
        }
      } catch (e: any) {
        failures += 1;
        if (failures >= GIVE_UP_AFTER) {
          set({ polling: false, error: `Stopped checking for updates: ${e?.message ?? "the server could not be reached"}.` });
        } else {
          schedule(() => poll(jobId));
        }
      }
    };

    setState((s) => ({ ...s, loading: true, error: null }));
    full().then((data) => {
      if (cancelled) return;
      const polling = active(data.job);
      setState({ ...data, loading: false, error: null, polling });
      if (polling && data.job) schedule(() => poll(data.job!.id));
    }).catch((e) => set({ loading: false, polling: false, error: e?.message ?? "The investigation could not be loaded." }));

    return () => { cancelled = true; clearTimeout(timer.current); };
  }, [simId, generation]);

  return { ...state, reload };
}
