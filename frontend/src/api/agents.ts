import { api } from "./client";
import { Agent, AgentGraph, AgentVersion } from "@/types/agent";

export const agentsApi = {
  list: () => api.get<Agent[]>("/api/agents").then((r) => r.data),
  get: (id: string) => api.get<Agent>(`/api/agents/${id}`).then((r) => r.data),
  create: (data: Partial<Agent>) => api.post<Agent>("/api/agents", data).then((r) => r.data),
  update: (id: string, data: Partial<Agent>) => api.patch<Agent>(`/api/agents/${id}`, data).then((r) => r.data),
  remove: (id: string) => api.delete(`/api/agents/${id}`).then((r) => r.data),
  versions: (id: string) => api.get<AgentVersion[]>(`/api/agents/${id}/versions`).then((r) => r.data),
  createVersion: (id: string, data: any) => api.post<AgentVersion>(`/api/agents/${id}/versions`, data).then((r) => r.data),
  promote: (id: string, data: { target_environment: string; version: string; force?: boolean }) =>
    api.post(`/api/agents/${id}/promote`, data).then((r) => r.data),
  graph: () => api.get<AgentGraph>("/api/agents/graph").then((r) => r.data),
};
