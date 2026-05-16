import { useNavigate } from "react-router-dom";
import { useAuthStore } from "@/stores/authStore";
import { LogOut, Radio } from "lucide-react";
import { useEffect } from "react";
import { useWsStore } from "@/stores/wsStore";
import { authApi } from "@/api/auth";

export default function Header() {
  const { user, role, refreshToken, clear } = useAuthStore();
  const { connected, connect } = useWsStore();
  const navigate = useNavigate();

  useEffect(() => {
    connect();
  }, [connect]);

  const onLogout = async () => {
    try {
      if (refreshToken) await authApi.logout(refreshToken);
    } finally {
      clear();
      navigate("/login");
    }
  };

  return (
    <header className="h-12 flex items-center justify-between px-4 border-b border-ink-800 bg-ink-950">
      <div className="text-sm text-ink-300">AI Agent Reliability & Observability</div>
      <div className="flex items-center gap-3 text-sm">
        <div className="flex items-center gap-1 text-xs text-ink-400">
          <Radio className={connected ? "w-3 h-3 text-emerald-400" : "w-3 h-3 text-rose-400"} />
          {connected ? "live" : "offline"}
        </div>
        <span className="text-ink-300">{user?.email}</span>
        <span className="text-ink-500 text-xs uppercase">{role}</span>
        <button onClick={onLogout} className="text-ink-400 hover:text-ink-100">
          <LogOut className="w-4 h-4" />
        </button>
      </div>
    </header>
  );
}
