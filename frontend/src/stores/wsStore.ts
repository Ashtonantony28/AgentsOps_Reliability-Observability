import { create } from "zustand";
import { WS_URL } from "@/api/client";
import { useAuthStore } from "@/stores/authStore";
import { Violation } from "@/types/policy";

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
  violations: Violation[];
  toasts: Violation[];
  socket: WebSocket | null;
  connect: () => void;
  disconnect: () => void;
  receive: (msg: any) => void;
  dismissToast: (v: Violation) => void;
};

export const useWsStore = create<WsState>((set, get) => ({
  connected: false,
  recent: [],
  violations: [],
  toasts: [],
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
        get().receive(JSON.parse(ev.data));
      } catch {}
    };
    set({ socket: ws });
  },
  disconnect: () => {
    const s = get().socket;
    if (s) s.close();
    set({ socket: null, connected: false });
  },
  receive: (msg) => {
    if (msg?.type === "trace") {
      set((s) => ({ recent: [msg.data, ...s.recent].slice(0, 100) }));
    } else if (msg?.type === "violation") {
      set((s) => ({
        violations: [msg.data, ...s.violations].slice(0, 100),
        toasts: [msg.data, ...s.toasts].slice(0, 3),
      }));
    }
  },
  dismissToast: (v) => set((s) => ({ toasts: s.toasts.filter((t) => t !== v) })),
}));
