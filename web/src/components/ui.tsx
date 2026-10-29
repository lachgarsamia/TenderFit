import { useEffect, type ReactNode } from "react";

type Style = { icon: string; label: string; tone: string };

const VERDICT: Record<string, Style> = {
  met: { icon: "✓", label: "Met", tone: "ok" },
  not_met: { icon: "✕", label: "Not met", tone: "bad" },
  needs_confirmation: { icon: "◷", label: "Needs confirmation", tone: "warn" },
  unknown: { icon: "?", label: "Unknown", tone: "outline" },
};
const EVIDENCE: Record<string, Style> = {
  document_on_file: { icon: "▤", label: "Document on file", tone: "info" },
  declared: { icon: "✎", label: "Declared", tone: "muted" },
  needs_update: { icon: "↻", label: "Needs update", tone: "warn" },
  missing: { icon: "✕", label: "Evidence missing", tone: "bad" },
  unknown: { icon: "?", label: "Unknown", tone: "outline" },
};
const STATUS: Record<string, Style> = {
  open: { icon: "●", label: "Open", tone: "status-ok" },
  unverified: { icon: "!", label: "Status unverified", tone: "status-warn" },
  closed: { icon: "■", label: "Closed", tone: "status-muted" },
  cancelled: { icon: "✕", label: "Cancelled", tone: "status-bad" },
  awarded: { icon: "◆", label: "Awarded", tone: "status-muted" },
};
const FIT: Record<string, Style> = {
  relevant: { icon: "◷", label: "Relevant", tone: "warn" },
  provisional: { icon: "!", label: "Provisional", tone: "warn" },
  blocker: { icon: "✕", label: "Possible blocker", tone: "bad" },
  low: { icon: "–", label: "Low relevance", tone: "muted" },
  unassessed: { icon: "○", label: "Not assessed", tone: "outline" },
};

function Pill({ s, small = true, children }: { s: Style; small?: boolean; children?: ReactNode }) {
  return <span className={`pill ${s.tone}${small ? " sm" : ""}`}><span className="i" aria-hidden>{s.icon}</span>{children ?? s.label}</span>;
}

export const VerdictPill = ({ status }: { status: string }) => <Pill s={VERDICT[status] ?? { icon: "·", label: status, tone: "muted" }} />;
export const EvidencePill = ({ status, children }: { status: string; children?: ReactNode }) =>
  <Pill s={EVIDENCE[status] ?? { icon: "·", label: status, tone: "muted" }}>{children}</Pill>;
export const FitPill = ({ kind, children }: { kind: string; children?: ReactNode }) => <Pill s={FIT[kind] ?? FIT.unassessed}>{children}</Pill>;
export const Tag = ({ tone = "muted", children }: { tone?: string; children: ReactNode }) => <span className={`pill ${tone} sm`}>{children}</span>;
export const Fictional = ({ children = "Demo data" }: { children?: ReactNode }) => <span className="pill fiction sm">{children}</span>;

export function OppStatus({ status, suffix }: { status: string; suffix?: string }) {
  const s = STATUS[status] ?? { icon: "·", label: status, tone: "status-muted" };
  return <span className={s.tone}><span aria-hidden>{s.icon}</span> {s.label}{suffix ? <span className="muted"> · {suffix}</span> : null}</span>;
}

export function Card({ title, note, children, flush, className = "" }: { title?: ReactNode; note?: ReactNode; children: ReactNode; flush?: boolean; className?: string }) {
  return (
    <section className={`card ${flush ? "flush" : ""} ${className}`}>
      {(title || note) && <header className="card-head"><h2>{title}</h2>{note && <span className="card-note">{note}</span>}</header>}
      {children}
    </section>
  );
}

export function Tile({ label, value, detail, tone = "" }: { label: string; value: ReactNode; detail?: ReactNode; tone?: string }) {
  return (
    <div className="tile">
      <div className="tile-label">{label}</div>
      <div className={`tile-value ${tone}`}>{value}</div>
      {detail && <div className="tile-detail" title={typeof detail === "string" ? detail : undefined}>{detail}</div>}
    </div>
  );
}

export function Tabs<T extends string>({ tabs, value, onChange, label }: { tabs: { id: T; label: string; count?: number }[]; value: T; onChange: (t: T) => void; label: string }) {
  return (
    <div className="tabs" role="tablist" aria-label={label}>
      {tabs.map((t) => (
        <button key={t.id} role="tab" className="tab" aria-selected={t.id === value} onClick={() => onChange(t.id)}>
          {t.label}{t.count !== undefined && <span className="n">{t.count}</span>}
        </button>
      ))}
    </div>
  );
}

export function Empty({ title, children }: { title?: string; children?: ReactNode }) {
  return <div className="empty">{title && <strong>{title}</strong>}{children}</div>;
}

export function Skeleton({ lines = 4 }: { lines?: number }) {
  return <div aria-busy="true" aria-label="Loading">{Array.from({ length: lines }, (_, i) => <div key={i} className="skeleton" style={{ width: `${90 - i * 12}%` }} />)}</div>;
}

export function ErrorNote({ error }: { error: string | null }) {
  return error ? <div className="error-note" role="alert">{human(error)}</div> : null;
}

export function Toast({ message, busy, onDone }: { message: string | null; busy?: boolean; onDone: () => void }) {
  useEffect(() => {
    if (message && !busy) { const t = setTimeout(onDone, 3500); return () => clearTimeout(t); }
  }, [message, busy]);
  return message ? <div className="toast" role="status">{busy && <span className="spinner" aria-hidden />}{message}</div> : null;
}

const ICONS: Record<string, string> = {
  search: "M11 4a7 7 0 1 0 4.2 12.6l4.1 4.1 1.4-1.4-4.1-4.1A7 7 0 0 0 11 4zm0 2a5 5 0 1 1 0 10 5 5 0 0 1 0-10z",
  list: "M4 6h2v2H4zm4 0h12v2H8zM4 11h2v2H4zm4 0h12v2H8zm-4 5h2v2H4zm4 0h12v2H8z",
  clock: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zm0 2a7 7 0 1 1 0 14 7 7 0 0 1 0-14zm-1 2v6l5 3 1-1.7-4-2.3V7z",
  building: "M4 21V5l8-3 8 3v16h-6v-5h-4v5zm4-12h2V7H8zm6 0h2V7h-2zm-6 4h2v-2H8zm6 0h2v-2h-2z",
  clipboard: "M9 3h6l1 2h3v16H5V5h3zm1 2-.5 1h5l-.5-1zM8 10h8v2H8zm0 4h8v2H8z",
  external: "M14 4h6v6h-2V7.4l-7.3 7.3-1.4-1.4L16.6 6H14zM5 6h6v2H7v9h9v-4h2v6H5z",
};

export function Icon({ name, size = 18 }: { name: keyof typeof ICONS; size?: number }) {
  return <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden fill="currentColor"><path d={ICONS[name]} /></svg>;
}

/** Turn internal reasons into plain language for suppliers. */
export function human(text?: string | null): string {
  if (!text) return "";
  if (/ANTHROPIC_API_KEY|model unavailable|agent unavailable/i.test(text))
    return "Not checked yet: the AI service is not connected. Nothing was guessed.";
  if (/budget reached/i.test(text)) return "Checking stopped at the cost limit for this lot. Nothing was guessed.";
  return text
    .replace(/\s*\[downgraded:[^\]]*\]/g, "")
    .replace(/\[on declared facts; proof required\]/g, "(declared only; proof is required)")
    .replace(/HTTP 5\d\d/g, "The server had a problem. Please try again.");
}
