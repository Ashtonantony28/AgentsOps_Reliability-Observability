export type Agent = {
  id: string;
  org_id: string;
  name: string;
  description: string;
  model_provider: string;
  model_name: string;
  environment: "dev" | "staging" | "prod";
  current_version: string;
  tags: string[];
  allowed_tools: string[];
  eval_threshold: number;
  created_at: string;
  updated_at: string;
  health: "healthy" | "degraded" | "down" | "unknown";
  error_rate: number;
  p95_latency_ms: number;
  cost_24h: number;
};

export type AgentVersion = {
  id: string;
  agent_id: string;
  version: string;
  model_name: string;
  prompt_template: string;
  config: Record<string, any>;
  environment: string;
  created_at: string;
};

export type AgentGraph = {
  nodes: Agent[];
  edges: { parent_id: string; child_id: string; call_count: number }[];
};
