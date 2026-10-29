import { createContext, useContext, useEffect, useState } from "react";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { api } from "./api";
import Assessment from "./pages/Assessment";
import Changes from "./pages/Changes";
import Opportunities from "./pages/Opportunities";
import Preparation from "./pages/Preparation";
import Profile from "./pages/Profile";

export type Profile = { id: number; name: string; regions: string[]; description: string; is_fictional: boolean };
type Ctx = { profile: Profile | null; profiles: Profile[]; setProfileId: (id: number) => void; refreshChanges: () => void };
const ProfileContext = createContext<Ctx>({ profile: null, profiles: [], setProfileId: () => {}, refreshChanges: () => {} });
export const useProfile = () => useContext(ProfileContext);

export default function App() {
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [profileId, setProfileId] = useState<number>(() => Number(localStorage.getItem("profileId") || 0));
  const [changes, setChanges] = useState(0);
  const profile = profiles.find((p) => p.id === profileId) ?? profiles[0] ?? null;

  useEffect(() => { api<Profile[]>("/profiles").then(setProfiles).catch(() => setProfiles([])); }, []);
  const refreshChanges = () => {
    if (profile) api<any[]>(`/changes?profile_id=${profile.id}`).then((c) => setChanges(c.length)).catch(() => {});
  };
  useEffect(refreshChanges, [profile?.id]);
  const choose = (id: number) => { localStorage.setItem("profileId", String(id)); setProfileId(id); };

  return (
    <ProfileContext.Provider value={{ profile, profiles, setProfileId: choose, refreshChanges }}>
      <div className="shell">
        <aside className="side">
          <div className="brand"><span className="brand-mark">✓</span> TenderFit</div>
          <nav className="nav">
            <NavLink to="/opportunities">Opportunities</NavLink>
            <NavLink to="/changes">Changes {changes > 0 && <span className="count">{changes}</span>}</NavLink>
            <NavLink to="/profile">Business profile</NavLink>
            <NavLink to="/preparation">Bid preparation</NavLink>
          </nav>
          <div className="workspace">
            <div>Workspace</div>
            <select value={profile?.id ?? ""} onChange={(e) => choose(Number(e.target.value))} aria-label="Business profile">
              {profiles.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
            </select>
            {profile?.is_fictional && <div className="fic">Fictional test profile</div>}
          </div>
        </aside>
        <main className="main">
          {!profile ? <div className="empty">Loading profiles… (is the API running on port 8000?)</div> : (
            <Routes>
              <Route path="/" element={<Navigate to="/opportunities" replace />} />
              <Route path="/opportunities" element={<Opportunities />} />
              <Route path="/lots/:lotId" element={<Assessment />} />
              <Route path="/changes" element={<Changes />} />
              <Route path="/profile" element={<Profile />} />
              <Route path="/preparation" element={<Preparation />} />
              <Route path="/lots/:lotId/preparation" element={<Preparation />} />
            </Routes>
          )}
        </main>
      </div>
    </ProfileContext.Provider>
  );
}
