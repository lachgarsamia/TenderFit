import { Tag } from "./ui";

/** Shared evidence vocabulary. An assumption always takes precedence over a document's historical verification. */
export function EvidenceBadge({ status, verified = false, origin = "actual" }: {
  status: string; verified?: boolean; origin?: string;
}) {
  if (origin === "modified" || origin === "hypothetical") return <Tag tone="warn">Assumption (this scenario)</Tag>;
  const label = status === "document_on_file" ? (verified ? "Verified evidence" : "Document on file, unchecked")
    : status === "declared" ? "Declared" : status === "needs_update" ? "Needs update" : "Missing / unknown";
  return <Tag tone={status === "document_on_file" && verified ? "ok" : "warn"}>{label}</Tag>;
}

export function verifiedSupport(s: { verified?: boolean; human_verified?: boolean; human_verified_by?: string | null; verification_level?: string | null }): boolean {
  return Boolean(s.human_verified || s.human_verified_by || (s.verified && ["supports", "current"].includes(s.verification_level ?? "")));
}
