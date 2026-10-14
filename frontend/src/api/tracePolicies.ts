import { api } from "./client";
import {
  PolicyError,
  PolicyValidateOut,
  TracePolicy,
  TracePolicyCreate,
  TracePolicyUpdate,
  Violation,
} from "@/types/policy";

export const tracePoliciesApi = {
  list: () => api.get<TracePolicy[]>("/api/trace-policies").then((r) => r.data),
  create: (data: TracePolicyCreate) => api.post<TracePolicy>("/api/trace-policies", data).then((r) => r.data),
  update: (id: string, data: TracePolicyUpdate) =>
    api.put<TracePolicy>(`/api/trace-policies/${id}`, data).then((r) => r.data),
  remove: (id: string) => api.delete(`/api/trace-policies/${id}`).then((r) => r.data),
  validate: (source: string) =>
    api.post<PolicyValidateOut>("/api/trace-policies/validate", { source }).then((r) => r.data),
  traceViolations: (traceId: string) =>
    api.get<Violation[]>(`/api/traces/${traceId}/violations`).then((r) => r.data),
};

// 422 invalid_policy bodies carry the parse errors under error.details.errors
export function policyErrors(err: any): PolicyError[] {
  const errors = err?.response?.data?.error?.details?.errors;
  return Array.isArray(errors) ? errors : [];
}
