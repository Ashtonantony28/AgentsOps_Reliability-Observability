import { api } from "./client";
import { AcceptInviteRequest, Invite, InviteRequest, PasswordChangeRequest, TokenPair } from "@/types/auth";
import { IntegrationCreated, SigningSecret } from "@/types/integration";

export const authApi = {
  register: (data: { email: string; password: string; full_name?: string; org_name?: string }) =>
    api.post("/api/auth/register", data).then((r) => r.data),
  login: (email: string, password: string) =>
    api.post("/api/auth/login", { email, password }).then((r) => r.data),
  refresh: (refresh_token: string) =>
    api.post("/api/auth/refresh", { refresh_token }).then((r) => r.data),
  logout: (refresh_token: string) =>
    api.post("/api/auth/logout", { refresh_token }).then((r) => r.data),
  me: () => api.get("/api/auth/me").then((r) => r.data),
  members: () => api.get("/api/auth/members").then((r) => r.data),
  invite: (data: InviteRequest) => api.post<Invite>("/api/auth/invite", data).then((r) => r.data),
  acceptInvite: (data: AcceptInviteRequest) =>
    api.post<TokenPair>("/api/auth/accept-invite", data).then((r) => r.data),
  changePassword: (data: PasswordChangeRequest) =>
    api.post<TokenPair>("/api/auth/password", data).then((r) => r.data),
  sessions: () => api.get("/api/auth/sessions").then((r) => r.data),
  revokeSession: (id: string) => api.delete(`/api/auth/sessions/${id}`).then((r) => r.data),
  listApiKeys: () => api.get("/api/auth/api-keys").then((r) => r.data),
  createApiKey: (name: string) => api.post("/api/auth/api-keys", { name }).then((r) => r.data),
  revokeApiKey: (id: string) => api.delete(`/api/auth/api-keys/${id}`).then((r) => r.data),
};

export const integrationsApi = {
  list: () => api.get("/api/integrations").then((r) => r.data),
  create: (data: any) => api.post<IntegrationCreated>("/api/integrations", data).then((r) => r.data),
  update: (id: string, data: any) => api.patch(`/api/integrations/${id}`, data).then((r) => r.data),
  remove: (id: string) => api.delete(`/api/integrations/${id}`).then((r) => r.data),
  test: (id: string) => api.post(`/api/integrations/${id}/test`).then((r) => r.data),
  rotateSigningSecret: (id: string) =>
    api.post<SigningSecret>(`/api/integrations/${id}/signing-secret`).then((r) => r.data),
};

export const governanceApi = {
  audit: (days = 30) => api.get("/api/governance/audit-log", { params: { days } }).then((r) => r.data),
  policies: () => api.get("/api/governance/policies").then((r) => r.data),
  createPolicy: (data: any) => api.post("/api/governance/policies", data).then((r) => r.data),
  deletePolicy: (id: string) => api.delete(`/api/governance/policies/${id}`).then((r) => r.data),
  leastPrivilege: () => api.get("/api/governance/least-privilege").then((r) => r.data),
  scorecard: () => api.get("/api/governance/scorecard").then((r) => r.data),
};

export const reportsApi = {
  summary: (days = 7) => api.get("/api/reports/summary", { params: { days } }).then((r) => r.data),
};
