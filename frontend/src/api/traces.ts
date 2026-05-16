import { api } from "./client";
import { CostBurn, Metrics, Trace } from "@/types/trace";

export const tracesApi = {
  list: (params: { agent_id?: string; status?: string; environment?: string; limit?: number; since_hours?: number } = {}) =>
    api.get<Trace[]>("/api/traces", { params }).then((r) => r.data),
  get: (id: string) => api.get<Trace>(`/api/traces/${id}`).then((r) => r.data),
  metrics: (params: { agent_id?: string; hours?: number } = {}) =>
    api.get<Metrics>("/api/traces/metrics", { params }).then((r) => r.data),
  costBurn: (params: { days?: number } = {}) =>
    api.get<CostBurn>("/api/traces/cost-burn", { params }).then((r) => r.data),
  exportCsv: (params: { agent_id?: string; hours?: number } = {}) =>
    `${api.defaults.baseURL}/api/traces/export.csv?${new URLSearchParams(params as any).toString()}`,
};
