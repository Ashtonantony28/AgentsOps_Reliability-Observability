import { api } from "./client";
import { EvalDiff, EvalRun, EvalRunDetail, EvalSet } from "@/types/evaluation";

export const evalsApi = {
  listSets: () => api.get<EvalSet[]>("/api/evaluations/sets").then((r) => r.data),
  getSet: (id: string) => api.get<EvalSet>(`/api/evaluations/sets/${id}`).then((r) => r.data),
  createSet: (data: any) => api.post<EvalSet>("/api/evaluations/sets", data).then((r) => r.data),
  addCase: (setId: string, data: any) =>
    api.post(`/api/evaluations/sets/${setId}/cases`, data).then((r) => r.data),
  runSet: (setId: string, data: { agent_id: string; agent_version?: string }) =>
    api.post<EvalRun>(`/api/evaluations/sets/${setId}/run`, data).then((r) => r.data),
  listRuns: (params: { agent_id?: string; set_id?: string } = {}) =>
    api.get<EvalRun[]>("/api/evaluations/runs", { params }).then((r) => r.data),
  getRun: (id: string) => api.get<EvalRunDetail>(`/api/evaluations/runs/${id}`).then((r) => r.data),
  diff: (run_a_id: string, run_b_id: string) =>
    api.post<EvalDiff>("/api/evaluations/diff", { run_a_id, run_b_id }).then((r) => r.data),
};
