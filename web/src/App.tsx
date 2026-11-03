import { createContext, useContext, useEffect, useState } from "react";
import { Link, NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { STATIC, SNAPSHOT_META, api, fmtDate } from "./api";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { Icon } from "./components/ui";
import Assessment from "./pages/Assessment";
import Changes from "./pages/Changes";
import Opportunities from "./pages/Opportunities";
import Preparation from "./pages/Preparation";
import Profile from "./pages/Profile";
import Tender from "./pages/Tender";

export type Profile = { id: number; name: string; regions: string[]; description: string; is_fictional: boolean };
type Ctx = { profile: Profile | null; refreshChanges: () => void };
const ProfileContext = createContext<Ctx>({ profile: null, refreshChanges: () => {} });
export const useProfile = () => useContext(ProfileContext);

export function usePageTitle(title: string) {
  useEffect(() => { document.title = title ? `${title} · TenderFit` : "TenderFit"; }, [title]);
}

export default function App() {
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [profileId, setProfileId] = useState<number>(() => Number(localStorage.getItem("profileId") || 0));
  const [changes, setChanges] = useState(0);
  const [failed, setFailed] = useState(false);
  const location = useLocation();
  const profile = profiles.find((p) => p.id === profileId) ?? profiles[0] ?? null;

  useEffect(() => { api<Profile[]>("/profiles").then(setProfiles).catch(() => setFailed(true)); }, []);
  const refreshChanges = () => {
    // Only what still needs attention counts; the page itself reports a failure to load.
    if (profile) api<any[]>(`/changes?profile_id=${profile.id}`)
      .then((c) => setChanges(c.filter((e) => e.status !== "superseded").length))
      .catch((e) => console.warn("Could not refresh the change count", e));
  };
  useEffect(refreshChanges, [profile?.id]);
  const choose = (id: number) => { localStorage.setItem("profileId", String(id)); setProfileId(id); };

  return (
    <ProfileContext.Provider value={{ profile, refreshChanges }}>
      <div className="shell">
        <aside className="side">
          <Link to="/opportunities" className="brand"><span className="brand-mark">✓</span> TenderFit</Link>
          <nav className="nav" aria-label="Main">
            <NavLink to="/opportunities"><Icon name="search" />Opportunities</NavLink>
            <NavLink to="/changes"><Icon name="clock" />Changes{changes > 0 && <span className="count" aria-label={`${changes} changes`}>{changes}</span>}</NavLink>
            <NavLink to="/profile"><Icon name="building" />Business profile</NavLink>
            <NavLink to="/preparation"><Icon name="clipboard" />Bid preparation</NavLink>
          </nav>
          {profiles.length > 0 && (
            <div className="workspace">
              <label htmlFor="ws">Workspace</label>
              <select id="ws" value={profile?.id ?? ""} onChange={(e) => choose(Number(e.target.value))}>
                {profiles.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
              </select>
              {profile?.is_fictional && <div className="fic">Fictional test profile.</div>}
            </div>
          )}
        </aside>
        <main className="main" id="main">
          {STATIC && <div className="banner info" role="note"><span className="b-icon">i</span><div className="body">
            <strong>Read-only demo</strong>
            <p>Tender notices are real data from Find a Tender, captured {fmtDate(SNAPSHOT_META?.captured_at)}. The three companies and the two notices marked Demo are fictional, and the AI answers on those notices are scripted. Buttons that change data are switched off.</p>
          </div></div>}
          {failed ? <div className="empty" role="alert"><strong>TenderFit can't reach its server.</strong>It may be restarting. Reload in a moment; if this stays, check that the API is running.</div> :
            !profile ? <div className="empty">{profiles.length === 0 && !failed ? "Loading…" : "No workspace found."}</div> : (
            <ErrorBoundary resetKey={location.pathname}><Routes>
              <Route path="/" element={<Navigate to="/opportunities" replace />} />
              <Route path="/opportunities" element={<Opportunities />} />
              <Route path="/tenders/:tenderId" element={<Tender />} />
              <Route path="/lots/:lotId" element={<Assessment />} />
              <Route path="/changes" element={<Changes />} />
              <Route path="/profile" element={<Profile />} />
              <Route path="/preparation" element={<Preparation />} />
              <Route path="/lots/:lotId/preparation" element={<Preparation />} />
              <Route path="*" element={<div className="empty"><strong>Page not found.</strong><Link to="/opportunities">Back to opportunities</Link></div>} />
            </Routes></ErrorBoundary>
          )}
        </main>
      </div>
    </ProfileContext.Provider>
  );
}
