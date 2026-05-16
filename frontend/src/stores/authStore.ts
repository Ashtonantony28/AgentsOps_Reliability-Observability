import { create } from "zustand";
import { persist } from "zustand/middleware";

type AuthState = {
  accessToken: string | null;
  refreshToken: string | null;
  user: { id: string; email: string; full_name: string } | null;
  orgId: string | null;
  role: string | null;
  setTokens: (access: string, refresh: string) => void;
  setUser: (u: AuthState["user"], orgId: string, role: string) => void;
  clear: () => void;
};

export const useAuthStore = create<AuthState>()(
  persist(
    (set) => ({
      accessToken: null,
      refreshToken: null,
      user: null,
      orgId: null,
      role: null,
      setTokens: (access, refresh) =>
        set({ accessToken: access, refreshToken: refresh }),
      setUser: (user, orgId, role) => set({ user, orgId, role }),
      clear: () =>
        set({ accessToken: null, refreshToken: null, user: null, orgId: null, role: null }),
    }),
    { name: "agentops-auth" }
  )
);
