import { useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { authApi } from "@/api/auth";
import { useAuthStore } from "@/stores/authStore";
import { errorMessage } from "@/api/client";

export default function AcceptInvite() {
  const [params] = useSearchParams();
  const token = params.get("token") || "";
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();
  const { setTokens, setUser } = useAuthStore();

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError(null);
    try {
      const data = await authApi.acceptInvite({ token, password });
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
        <h1 className="text-lg font-medium mb-4">Accept invite</h1>
        {!token ? (
          <div className="text-sm text-rose-300">This invite link has no token.</div>
        ) : (
          <form onSubmit={submit} className="space-y-3">
            <input
              type="password"
              className="w-full bg-ink-950 border border-ink-700 rounded px-3 py-2 text-sm"
              placeholder="Choose a password (8+ characters)"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              minLength={8}
              maxLength={128}
              required
            />
            {error && <div className="text-xs text-rose-300">{error}</div>}
            <button
              type="submit"
              disabled={loading || password.length < 8}
              className="w-full bg-brand-600 hover:bg-brand-500 disabled:opacity-50 text-white rounded py-2 text-sm font-medium"
            >
              {loading ? "..." : "Join"}
            </button>
          </form>
        )}
      </div>
    </div>
  );
}
