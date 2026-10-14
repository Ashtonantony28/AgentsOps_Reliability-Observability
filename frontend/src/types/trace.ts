export type Span = {
  id: string;
  trace_id: string;
  parent_span_id: string | null;
  seq?: number | null;
  kind: string;
  name: string;
  input: Record<string, any>;
  output: Record<string, any>;
  attrs?: Record<string, any> | null;
  latency_ms: number;
  status: string;
  started_at: string;
  ended_at: string | null;
};

export type Trace = {
  id: string;
  org_id: string;
  agent_id: string;
  session_id: string | null;
  trace_uid: string;
  status: string;
  input: string;
  output: string;
  error_message: string | null;
  latency_ms: number;
  tokens_input: number;
  tokens_output: number;
  cost_usd: number;
  environment: string;
  agent_version: string;
  pii_flags: string[];
  injection_score: number;
  meta: Record<string, any>;
  created_at: string;
  spans?: Span[];
};

export type TimeSeriesPoint = { t: string; value: number };

export type Metrics = {
  success_rate: TimeSeriesPoint[];
  error_rate: TimeSeriesPoint[];
  latency_avg_ms: TimeSeriesPoint[];
  latency_p95_ms: TimeSeriesPoint[];
  tokens: TimeSeriesPoint[];
  cost_usd: TimeSeriesPoint[];
  totals: Record<string, number>;
};

export type CostBurn = {
  daily: TimeSeriesPoint[];
  monthly_projection_usd: number;
  by_agent: { agent_id: string; agent_name: string; cost_usd: number }[];
};
