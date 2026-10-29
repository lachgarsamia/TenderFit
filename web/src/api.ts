export const STATIC = import.meta.env.VITE_STATIC === "1";
const SNAPSHOT: Record<string, any> = STATIC
  ? (Object.values(import.meta.glob("../demo/snapshot.json", { eager: true, import: "default" }))[0] as any) ?? {}
  : {};
export const SNAPSHOT_META = SNAPSHOT.__meta ?? null;

function fromSnapshot<T>(path: string, init?: RequestInit): T {
  const method = (init?.method ?? "GET").toUpperCase();
  if (method === "POST" && path === "/search") {
    const body = JSON.parse(String(init?.body ?? "{}"));
    const hit = SNAPSHOT[`POST /search?profile_id=${body.profile_id}`];
    if (hit) return hit;
  }
  if (method !== "GET") throw new Error("This is a read-only demo, so actions that change data are switched off.");
  if (path in SNAPSHOT) return SNAPSHOT[path];
  if (path.includes("/assessment?")) throw new Error("no assessment yet");
  throw new Error("This page is not part of the demo snapshot.");
}

export async function api<T = any>(path: string, init?: RequestInit): Promise<T> {
  if (STATIC) return fromSnapshot<T>(path, init);
  const r = await fetch(`/api${path}`, {
    ...init,
    headers: init?.body && !(init.body instanceof FormData) ? { "content-type": "application/json", ...init?.headers } : init?.headers,
  });
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    throw new Error(body.detail ?? `HTTP ${r.status}`);
  }
  return r.json();
}

export const post = <T = any>(path: string, body: unknown) => api<T>(path, { method: "POST", body: JSON.stringify(body) });

export async function waitForJobs(ids: number[], onTick?: () => void): Promise<void> {
  for (let i = 0; i < 120 && ids.length; i++) {
    await new Promise((r) => setTimeout(r, 1500));
    const jobs = await Promise.all(ids.map((id) => api(`/jobs/${id}`)));
    onTick?.();
    if (jobs.every((j) => j.status === "done" || j.status === "failed")) {
      const failed = jobs.find((j) => j.status === "failed");
      if (failed) throw new Error(failed.last_error?.split("\n").filter(Boolean).pop() ?? "job failed");
      return;
    }
  }
}

export function fmtDate(d?: string | null, withTime = true): string {
  if (!d) return "not stated";
  const dt = new Date(d);
  return dt.toLocaleString("en-GB", {
    day: "numeric", month: "short", year: "numeric", ...(withTime ? { hour: "2-digit", minute: "2-digit" } : {}),
    timeZone: "Europe/London",
  });
}

export function daysLeft(d?: string | null): number | null {
  if (!d) return null;
  return Math.ceil((new Date(d).getTime() - Date.now()) / 86_400_000);
}
