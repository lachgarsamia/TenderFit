import type { ReactNode } from "react";

const VERDICT: Record<string, { icon: string; label: string; tone: string }> = {
  met: { icon: "✓", label: "Met", tone: "ok" },
  not_met: { icon: "✕", label: "Not met", tone: "bad" },
  needs_confirmation: { icon: "◷", label: "Needs confirmation", tone: "warn" },
  unknown: { icon: "?", label: "Unknown", tone: "outline" },
};

export function VerdictBadge({ status, small }: { status: string; small?: boolean }) {
  const v = VERDICT[status] ?? { icon: "·", label: status, tone: "muted" };
  return <span className={`badge ${v.tone}${small ? " sm" : ""}`}><span aria-hidden>{v.icon}</span> {v.label}</span>;
}

const EVIDENCE: Record<string, { icon: string; label: string; tone: string }> = {
  document_on_file: { icon: "▤", label: "Document on file", tone: "info" },
  declared: { icon: "✎", label: "Declared", tone: "muted" },
  needs_update: { icon: "↻", label: "Needs update", tone: "warn" },
  missing: { icon: "✕", label: "Missing", tone: "bad" },
  unknown: { icon: "?", label: "Unknown", tone: "outline" },
};

export function EvidenceBadge({ status, children }: { status: string; children?: ReactNode }) {
  const v = EVIDENCE[status] ?? { icon: "·", label: status, tone: "muted" };
  return <span className={`badge ${v.tone} sm`}><span aria-hidden>{v.icon}</span> {children ?? v.label}</span>;
}

const STATUS: Record<string, { icon: string; label: string; tone: string }> = {
  open: { icon: "●", label: "Open", tone: "ok-text" },
  unverified: { icon: "!", label: "Status unverified", tone: "warn-text" },
  closed: { icon: "■", label: "Closed", tone: "muted-text" },
  cancelled: { icon: "✕", label: "Cancelled", tone: "bad-text" },
  awarded: { icon: "◆", label: "Awarded", tone: "muted-text" },
};

export function OppStatus({ status, suffix }: { status: string; suffix?: string }) {
  const s = STATUS[status] ?? { icon: "·", label: status, tone: "muted-text" };
  return <span className={s.tone}><span aria-hidden>{s.icon}</span> {s.label}{suffix ? ` · ${suffix}` : ""}</span>;
}

const FIT: Record<string, { icon: string; tone: string }> = {
  relevant: { icon: "◷", tone: "warn" },
  provisional: { icon: "!", tone: "warn" },
  blocker: { icon: "✕", tone: "bad" },
  low: { icon: "–", tone: "muted" },
  unassessed: { icon: "○", tone: "outline" },
};

export function FitBadge({ kind, children }: { kind: string; children: ReactNode }) {
  const f = FIT[kind] ?? FIT.unassessed;
  return <span className={`badge ${f.tone} sm`}><span aria-hidden>{f.icon}</span> {children}</span>;
}

export function Tag({ tone = "muted", children }: { tone?: string; children: ReactNode }) {
  return <span className={`badge ${tone} sm`}>{children}</span>;
}

export function Fictional({ children = "Fictional test data" }: { children?: ReactNode }) {
  return <span className="badge fiction sm">{children}</span>;
}

export function Card({ title, extra, children, className = "" }: { title?: ReactNode; extra?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`card ${className}`}>
      {(title || extra) && <header className="card-head"><h2>{title}</h2>{extra && <div className="card-extra">{extra}</div>}</header>}
      {children}
    </section>
  );
}

export function Tile({ label, value, detail, tone = "" }: { label: string; value: ReactNode; detail?: ReactNode; tone?: string }) {
  return (
    <div className="tile">
      <div className="tile-label">{label}</div>
      <div className={`tile-value ${tone}`}>{value}</div>
      {detail && <div className="tile-detail">{detail}</div>}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function ErrorNote({ error }: { error: string | null }) {
  return error ? <div className="error-note" role="alert">{error}</div> : null;
}
