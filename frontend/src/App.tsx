import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import { ReactNode, useEffect } from "react";
import Layout from "@/components/layout/Layout";
import Login from "@/pages/Login";
import AcceptInvite from "@/pages/AcceptInvite";
import Dashboard from "@/pages/Dashboard";
import Agents from "@/pages/Agents";
import AgentDetail from "@/pages/AgentDetail";
import Traces from "@/pages/Traces";
import TraceDetail from "@/pages/TraceDetail";
import Evaluations from "@/pages/Evaluations";
import EvaluationDetail from "@/pages/EvaluationDetail";
import Alerts from "@/pages/Alerts";
import Incidents from "@/pages/Incidents";
import Governance from "@/pages/Governance";
import Integrations from "@/pages/Integrations";
import Reports from "@/pages/Reports";
import Settings from "@/pages/Settings";
import { useAuthStore } from "@/stores/authStore";
import { authApi } from "@/api/auth";

function RequireAuth({ children }: { children: ReactNode }) {
  const token = useAuthStore((s) => s.accessToken);
  const user = useAuthStore((s) => s.user);
  const setUser = useAuthStore((s) => s.setUser);
  const location = useLocation();

  useEffect(() => {
    if (token && !user) {
      authApi.me()
        .then((me: any) => setUser({ id: me.user.id, email: me.user.email, full_name: me.user.full_name }, me.active_org_id, me.role))
        .catch(() => useAuthStore.getState().clear());
    }
  }, [token, user, setUser]);

  if (!token) return <Navigate to="/login" state={{ from: location }} replace />;
  return <>{children}</>;
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/accept-invite" element={<AcceptInvite />} />
      <Route
        path="/"
        element={
          <RequireAuth>
            <Layout />
          </RequireAuth>
        }
      >
        <Route index element={<Dashboard />} />
        <Route path="agents" element={<Agents />} />
        <Route path="agents/:id" element={<AgentDetail />} />
        <Route path="traces" element={<Traces />} />
        <Route path="traces/:id" element={<TraceDetail />} />
        <Route path="evaluations" element={<Evaluations />} />
        <Route path="evaluations/:id" element={<EvaluationDetail />} />
        <Route path="alerts" element={<Alerts />} />
        <Route path="incidents" element={<Incidents />} />
        <Route path="governance" element={<Governance />} />
        <Route path="integrations" element={<Integrations />} />
        <Route path="reports" element={<Reports />} />
        <Route path="settings" element={<Settings />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
