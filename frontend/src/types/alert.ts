export type AlertRule = {
  id: string;
  org_id: string;
  agent_id: string | null;
  name: string;
  metric: string;
  operator: string;
  threshold: number;
  window_minutes: number;
  severity: string;
  channels: Record<string, any>[];
  runbook_url: string;
  dedup_minutes: number;
  enabled: boolean;
  created_at: string;
};

export type AlertEvent = {
  id: string;
  rule_id: string;
  org_id: string;
  agent_id: string | null;
  status: string;
  value: number;
  message: string;
  assignee_id: string | null;
  fired_at: string;
  acknowledged_at: string | null;
  resolved_at: string | null;
  context: Record<string, any>;
};

export type Incident = {
  id: string;
  org_id: string;
  agent_id: string | null;
  title: string;
  status: string;
  severity: string;
  timeline: { ts: string; event: string; detail?: string }[];
  narrative: string;
  started_at: string;
  resolved_at: string | null;
};
