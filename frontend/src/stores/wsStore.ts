import { create } from "zustand";
import { WS_URL } from "@/api/client";
import { useAuthStore } from "@/stores/authStore";

type TraceEvent = {
  id: string;
  agent_id: string;
  status: string;
  latency_ms: number;
  cost_usd: number;
  created_at: string;
  input_preview: string;
};

type WsState = {
  connected: boolean;
  recent: TraceEvent[];
  socket: WebSocket | null;
  connect: () => void;
  disconnect: () => void;
};

export const useWsStore = create<WsState>((set, get) => ({
  connected: false,
  recent: [],
  socket: null,
  connect: () => {
    const existing = get().socket;
    if (existing && existing.readyState !== WebSocket.CLOSED) return;
    const token = useAuthStore.getState().accessToken;
    if (!token) return;
    const ws = new WebSocket(`${WS_URL}/ws/traces?token=${encodeURIComponent(token)}`);
    ws.onopen = () => set({ connected: true });
    ws.onclose = () => set({ connected: false });
    ws.onerror = () => set({ connected: false });
    ws.onmessage = (ev) => {
      try {
        const parsed = JSON.parse(ev.data);
        if (parsed.type === "trace") {
          set((s) => ({ recent: [parsed.data, ...s.recent].slice(0, 100) }));
        }
      } catch {}
    };
    set({ socket: ws });
  },
  disconnect: () => {
    const s = get().socket;
    if (s) s.close();
    set({ socket: null, connected: false });
  },
}));
