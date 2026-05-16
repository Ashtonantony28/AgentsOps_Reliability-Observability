import { api } from "./client";
import { AlertEvent, AlertRule, Incident } from "@/types/alert";

export const alertsApi = {
  listRules: () => api.get<AlertRule[]>("/api/alerts/rules").then((r) => r.data),
  createRule: (data: any) => api.post<AlertRule>("/api/alerts/rules", data).then((r) => r.data),
  updateRule: (id: string, data: any) => api.patch<AlertRule>(`/api/alerts/rules/${id}`, data).then((r) => r.data),
  deleteRule: (id: string) => api.delete(`/api/alerts/rules/${id}`).then((r) => r.data),
  listEvents: (status?: string) =>
    api.get<AlertEvent[]>("/api/alerts/events", { params: status ? { status } : {} }).then((r) => r.data),
  ack: (id: string) => api.post(`/api/alerts/events/${id}/ack`).then((r) => r.data),
  resolve: (id: string) => api.post(`/api/alerts/events/${id}/resolve`).then((r) => r.data),
};

export const incidentsApi = {
  list: (status?: string) =>
    api.get<Incident[]>("/api/incidents", { params: status ? { status } : {} }).then((r) => r.data),
  get: (id: string) => api.get<Incident>(`/api/incidents/${id}`).then((r) => r.data),
  resolve: (id: string) => api.post(`/api/incidents/${id}/resolve`).then((r) => r.data),
};
