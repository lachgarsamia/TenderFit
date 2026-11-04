import { useEffect, useState } from "react";
import { api, post } from "../api";
import { ErrorNote } from "../components/ui";

export function SignIn({ onDone }: { onDone: (me: any) => void }) {
  const [mode, setMode] = useState<"in" | "up">("in");
  const [config, setConfig] = useState<{ registration_enabled: boolean; demo: boolean } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { api("/auth/config").then(setConfig).catch((e) => setError(e.message)); }, []);

  const submit = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const d = new FormData(e.currentTarget);
    setBusy(true); setError(null);
    try {
      onDone(await post(mode === "in" ? "/auth/login" : "/auth/register", { email: d.get("email"), password: d.get("password") }));
    } catch (err: any) { setError(err.message); } finally { setBusy(false); }
  };
  return (
    <main className="centered">
      <div className="auth-card">
        <div className="brand dark"><span className="brand-mark">✓</span> TenderFit</div>
        <h1>{mode === "in" ? "Sign in" : "Create an account"}</h1>
        {config?.demo && <p className="small muted">Demo mode: fictional suppliers and scripted AI answers. The sign-in details are in docs/DEMO.md.</p>}
        <ErrorNote error={error} />
        <form onSubmit={submit}>
          <div className="field"><label htmlFor="email">Email</label><input id="email" name="email" type="email" autoComplete="username" required /></div>
          <div className="field"><label htmlFor="password">Password</label>
            <input id="password" name="password" type="password" autoComplete={mode === "in" ? "current-password" : "new-password"} minLength={mode === "up" ? 10 : undefined} required />
            {mode === "up" && <div className="small muted">At least 10 characters.</div>}</div>
          <button className="btn primary full" disabled={busy}>{busy ? "Please wait…" : mode === "in" ? "Sign in" : "Create account"}</button>
        </form>
        {config?.registration_enabled && <button className="link-btn dark" onClick={() => { setMode(mode === "in" ? "up" : "in"); setError(null); }}>
          {mode === "in" ? "Create an account" : "I already have an account"}</button>}
        {config && !config.registration_enabled && <p className="small muted">Accounts are created by the operator.</p>}
      </div>
    </main>
  );
}

/** Regions are ITL codes because the notices use them. Typed as UKE, UKD. */
const parseRegions = (s: string) => s.split(",").map((r) => r.trim().toUpperCase()).filter(Boolean);

export function ProfileFields({ initial }: { initial?: { name: string; regions: string[]; description: string } }) {
  return <>
    <div className="field"><label htmlFor="p-name">Supplier name</label><input id="p-name" name="name" defaultValue={initial?.name} required minLength={2} maxLength={200} /></div>
    <div className="field"><label htmlFor="p-regions">Delivery regions (ITL codes, comma separated)</label><input id="p-regions" name="regions" defaultValue={initial?.regions.join(", ")} placeholder="UKE, UKD" /></div>
    <div className="field"><label htmlFor="p-desc">What you supply</label><textarea id="p-desc" name="description" defaultValue={initial?.description} maxLength={2000} /></div>
  </>;
}

export const profileBody = (d: FormData) => ({ name: d.get("name"), regions: parseRegions(String(d.get("regions") ?? "")), description: d.get("description") ?? "" });

export function CreateProfile({ email, onDone, onSignOut }: { email: string; onDone: (me: any) => void; onSignOut: () => void }) {
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const submit = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    setBusy(true); setError(null);
    try { onDone(await post("/profiles", profileBody(new FormData(e.currentTarget)))); }
    catch (err: any) { setError(err.message); } finally { setBusy(false); }
  };
  return (
    <main className="centered">
      <div className="auth-card wide">
        <div className="brand dark"><span className="brand-mark">✓</span> TenderFit</div>
        <h1>Set up your supplier profile</h1>
        <p className="small muted">Signed in as {email}. You will add facts and evidence next. Only people on this profile can see it.</p>
        <ErrorNote error={error} />
        <form onSubmit={submit}>
          <ProfileFields />
          <button className="btn primary full" disabled={busy}>{busy ? "Creating…" : "Create profile"}</button>
        </form>
        <button className="link-btn dark" onClick={onSignOut}>Sign out</button>
      </div>
    </main>
  );
}
