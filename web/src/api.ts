export async function api<T = any>(path: string, init?: RequestInit): Promise<T> {
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
