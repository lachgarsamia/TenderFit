import { createContext, useContext, useEffect, useState } from "react";
import { Link, NavLink, Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { ApiError, SIGNED_OUT, STATIC, SNAPSHOT_META, api, fmtDate, post } from "./api";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { Icon, Skeleton } from "./components/ui";
import Assessment from "./pages/Assessment";
import Changes from "./pages/Changes";
import { CreateProfile, SignIn } from "./pages/Account";
import Opportunities from "./pages/Opportunities";
import Preparation from "./pages/Preparation";
import Profile from "./pages/Profile";
import Tender from "./pages/Tender";

export type Profile = { id: number; name: string; regions: string[]; description: string; is_fictional: boolean };
type Me = { email: string; profiles: Profile[]; active_profile_id: number | null };
type Ctx = { profile: Profile | null; refreshChanges: () => void; refreshMe: () => Promise<void> };
const ProfileContext = createContext<Ctx>({ profile: null, refreshChanges: () => {}, refreshMe: async () => {} });
export const useProfile = () => useContext(ProfileContext);

export function usePageTitle(title: string) {
  useEffect(() => { document.title = title ? `${title} · TenderFit` : "TenderFit"; }, [title]);
}

export default function App() {
  const [me, setMe] = useState<Me | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "signed-out" | "unreachable">("loading");
  const [changes, setChanges] = useState(0);
  const location = useLocation();
  const navigate = useNavigate();
  const profile = me?.profiles.find((p) => p.id === me.active_profile_id) ?? null;

  const refreshMe = async () => {
    try {
      setMe(await api<Me>("/auth/me")); setState("ready");
    } catch (e) {
      setMe(null);
      setState(e instanceof ApiError && e.status === 401 ? "signed-out" : "unreachable");
    }
  };
  useEffect(() => { refreshMe(); }, []);
  useEffect(() => {
    const onSignedOut = () => { setMe(null); setState("signed-out"); };
    window.addEventListener(SIGNED_OUT, onSignedOut);
    return () => window.removeEventListener(SIGNED_OUT, onSignedOut);
  }, []);

  const refreshChanges = () => {
    if (profile) api<any[]>("/changes")
      .then((c) => setChanges(c.filter((e) => e.status !== "superseded").length))
      .catch((e) => console.warn("Could not refresh the change count", e));
  };
  useEffect(refreshChanges, [profile?.id]);

  const choose = async (id: number) => {
    setMe(await post<Me>("/auth/active-profile", { profile_id: id }));
    navigate("/opportunities");
  };
  const signOut = async () => {
    await post("/auth/logout", {}).catch((e) => console.warn("Sign-out request failed", e));
    setMe(null); setChanges(0); setState("signed-out"); navigate("/");
  };

  if (state === "loading") return <main className="centered"><div style={{ width: 320 }}><Skeleton lines={3} /></div></main>;
  if (state === "unreachable") return <main className="centered"><div className="empty" role="alert"><strong>TenderFit can't reach its server.</strong>It may be restarting. Reload in a moment; if this stays, check that the API is running.</div></main>;
  if (state === "signed-out" || !me) return <SignIn onDone={(m) => { setMe(m); setState("ready"); }} />;
  if (me.profiles.length === 0) return <CreateProfile email={me.email} onDone={(m) => setMe(m)} onSignOut={signOut} />;

  return (
    <ProfileContext.Provider value={{ profile, refreshChanges, refreshMe }}>
      <div className="shell">
        <aside className="side">
          <Link to="/opportunities" className="brand"><span className="brand-mark">✓</span> TenderFit</Link>
          <nav className="nav" aria-label="Main">
            <NavLink to="/opportunities"><Icon name="search" />Opportunities</NavLink>
            <NavLink to="/changes"><Icon name="clock" />Changes{changes > 0 && <span className="count" aria-label={`${changes} changes`}>{changes}</span>}</NavLink>
            <NavLink to="/profile"><Icon name="building" />Business profile</NavLink>
            <NavLink to="/preparation"><Icon name="clipboard" />Bid preparation</NavLink>
          </nav>
          <div className="workspace">
            {me.profiles.length > 1 ? <>
              <label htmlFor="ws">Supplier</label>
              <select id="ws" value={profile?.id ?? ""} onChange={(e) => choose(Number(e.target.value))}>
                {!profile && <option value="">Choose…</option>}
                {me.profiles.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
              </select>
            </> : <><div className="ws-label">Supplier</div><div className="ws-name">{profile?.name}</div></>}
            {profile?.is_fictional && <div className="fic">Fictional test profile.</div>}
            <div className="ws-user"><span title={me.email}>{me.email}</span>{!STATIC && <button className="link-btn" onClick={signOut}>Sign out</button>}</div>
          </div>
        </aside>
        <main className="main" id="main">
          {STATIC && <div className="banner info" role="note"><span className="b-icon">i</span><div className="body">
            <strong>Read-only demo</strong>
            <p>Captured {fmtDate(SNAPSHOT_META?.captured_at)}. The companies and notices marked Demo are fictional, and the AI answers on them are scripted. Buttons that change data are switched off.</p>
          </div></div>}
          {!profile ? <div className="empty"><strong>Choose a supplier</strong>Pick one in the menu to continue.</div> : (
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
