import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { authApi } from "@/api/auth";
import { useAuthStore } from "@/stores/authStore";
import { errorMessage } from "@/api/client";
import { GitBranch } from "lucide-react";

export default function Login() {
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("demo@agentops.dev");
  const [password, setPassword] = useState("Demo1234!");
  const [fullName, setFullName] = useState("");
  const [orgName, setOrgName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();
  const { setTokens, setUser } = useAuthStore();

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError(null);
    try {
      const data =
        mode === "login"
          ? await authApi.login(email, password)
          : await authApi.register({ email, password, full_name: fullName, org_name: orgName });
      setTokens(data.access_token, data.refresh_token);
      const me = await authApi.me();
      setUser({ id: me.user.id, email: me.user.email, full_name: me.user.full_name }, me.active_org_id, me.role);
      navigate("/");
    } catch (err: any) {
      setError(errorMessage(err));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center bg-ink-950 text-ink-100">
      <div className="w-full max-w-sm bg-ink-900 border border-ink-800 rounded-lg p-6">
        <div className="flex items-center gap-2 mb-6">
          <GitBranch className="w-5 h-5 text-brand-500" />
          <span className="font-semibold tracking-tight">AgentOps</span>
        </div>
        <h1 className="text-lg font-medium mb-4">
          {mode === "login" ? "Sign in" : "Create account"}
        </h1>
        <form onSubmit={submit} className="space-y-3">
          {mode === "register" && (
            <>
              <input
                className="w-full bg-ink-950 border border-ink-700 rounded px-3 py-2 text-sm"
                placeholder="Full name"
                value={fullName}
                onChange={(e) => setFullName(e.target.value)}
              />
              <input
                className="w-full bg-ink-950 border border-ink-700 rounded px-3 py-2 text-sm"
                placeholder="Organization name"
                value={orgName}
                onChange={(e) => setOrgName(e.target.value)}
              />
            </>
          )}
          <input
            type="email"
            className="w-full bg-ink-950 border border-ink-700 rounded px-3 py-2 text-sm"
            placeholder="Email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
          />
          <input
            type="password"
            className="w-full bg-ink-950 border border-ink-700 rounded px-3 py-2 text-sm"
            placeholder="Password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
          {error && <div className="text-xs text-rose-300">{error}</div>}
          <button
            type="submit"
            disabled={loading}
            className="w-full bg-brand-600 hover:bg-brand-500 disabled:opacity-50 text-white rounded py-2 text-sm font-medium"
          >
            {loading ? "..." : mode === "login" ? "Sign in" : "Create account"}
          </button>
        </form>
        <button
          className="w-full mt-3 text-xs text-ink-400 hover:text-ink-200"
          onClick={() => setMode(mode === "login" ? "register" : "login")}
        >
          {mode === "login" ? "Need an account? Register" : "Have an account? Sign in"}
        </button>
        <div className="mt-4 text-[10px] text-ink-500 text-center">
          Demo: demo@agentops.dev / Demo1234!
        </div>
      </div>
    </div>
  );
}
