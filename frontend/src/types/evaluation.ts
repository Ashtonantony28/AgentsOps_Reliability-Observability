export type EvalCase = {
  id: string;
  input: string;
  expected_output: string;
  tags: string[];
  weight: number;
};

export type WebhookRunner = {
  type: "webhook";
  url: string;
  timeout_s?: number;
};

export type TracesRunner = {
  type: "traces";
  match?: "exact" | "contains";
  lookback_days?: number;
};

// null: no output source, so every case fails
export type EvalRunner = WebhookRunner | TracesRunner;

export type EvalSet = {
  id: string;
  org_id: string;
  name: string;
  description: string;
  evaluators: string[];
  schedule_cron: string | null;
  runner: EvalRunner | null;
  created_at: string;
  case_count: number;
  cases?: EvalCase[];
};

export type EvalRun = {
  id: string;
  set_id: string;
  agent_id: string;
  agent_version: string;
  status: string;
  score: number;
  passed: number;
  failed: number;
  total: number;
  summary: Record<string, any>;
  triggered_by: string | null;
  created_at: string;
  completed_at: string | null;
};

export type EvalResult = {
  id: string;
  run_id: string;
  case_id: string;
  actual_output: string;
  passed: boolean;
  score: number;
  scores_by_evaluator: Record<string, number>;
  reason: string;
  latency_ms: number;
};

export type EvalRunDetail = EvalRun & { results: EvalResult[] };

export type EvalDiff = {
  run_a: EvalRun;
  run_b: EvalRun;
  delta_score: number;
  regressions: { case_id: string; from: number; to: number }[];
  improvements: { case_id: string; from: number; to: number }[];
};
