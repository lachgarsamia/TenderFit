export const STATIC = import.meta.env.VITE_STATIC === "1";
const SNAPSHOT: Record<string, any> = STATIC
  ? (Object.values(import.meta.glob("../demo/snapshot.json", { eager: true, import: "default" }))[0] as any) ?? {}
  : {};
export const SNAPSHOT_META = SNAPSHOT.__meta ?? null;

let staticProfile: number | null = SNAPSHOT.profiles?.[0]?.id ?? null;
const staticMe = () => ({ email: "read-only demo", profiles: SNAPSHOT.profiles ?? [], active_profile_id: staticProfile });

function fromSnapshot<T>(path: string, init?: RequestInit): T {
  const method = (init?.method ?? "GET").toUpperCase();
  if (path === "/auth/me") return staticMe() as T;
  if (method === "POST" && path === "/auth/active-profile") {
    staticProfile = JSON.parse(String(init?.body)).profile_id;
    return staticMe() as T;
  }
  const supplier = SNAPSHOT.suppliers?.[String(staticProfile)] ?? {};
  if (method === "POST" && path === "/search" && supplier["POST /search"]) return supplier["POST /search"];
  if (method !== "GET") throw new Error("This is a read-only demo, so actions that change data are switched off.");
  if (path in supplier) return supplier[path];
  if (path.endsWith("/assessment")) throw new Error("no assessment yet");
  throw new Error("This page is not part of the demo snapshot.");
}

export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

const BASE = "/api/v1";
/** Announced when a session ends, so the whole app returns to sign-in instead of each page failing on its own. */
export const SIGNED_OUT = "tenderfit:signed-out";

export async function api<T = any>(path: string, init?: RequestInit): Promise<T> {
  if (STATIC) return fromSnapshot<T>(path, init);
  const r = await fetch(`${BASE}${path}`, {
    ...init,
    credentials: "same-origin",
    headers: { "X-TenderFit": "1", ...(init?.body && !(init.body instanceof FormData) ? { "content-type": "application/json" } : {}), ...init?.headers },
  });
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    if (r.status === 401 && !path.startsWith("/auth/")) window.dispatchEvent(new Event(SIGNED_OUT));
    throw new ApiError(typeof body.detail === "string" ? body.detail : Array.isArray(body.detail) ? body.detail[0]?.msg ?? `HTTP ${r.status}` : `HTTP ${r.status}`, r.status);
  }
  return r.json();
}

export const fileUrl = (documentId: number) => `${BASE}/documents/${documentId}/file`;

export const post = <T = any>(path: string, body: unknown) => api<T>(path, { method: "POST", body: JSON.stringify(body) });

export async function waitForJobs(ids: number[], onTick?: () => void): Promise<void> {
  if (!ids.length) return;
  for (let i = 0; i < 400; i++) {
    await new Promise((r) => setTimeout(r, 1500));
    const jobs = await Promise.all(ids.map((id) => api(`/jobs/${id}`)));
    onTick?.();
    if (jobs.every((j) => j.status === "done" || j.status === "failed")) {
      const failed = jobs.find((j) => j.status === "failed");
      if (failed) throw new Error(failed.last_error?.split("\n").filter(Boolean).pop() ?? "job failed");
      return;
    }
  }
  throw new Error("This is taking longer than expected. It is still running; check back in a few minutes.");
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
