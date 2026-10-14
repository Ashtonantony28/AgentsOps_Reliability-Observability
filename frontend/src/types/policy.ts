export type EventKind = "tool" | "llm" | "retrieval" | "confirmation" | "custom";
export type PolicySeverity = "info" | "low" | "medium" | "high" | "critical";
export type PolicyAction = "log" | "alert" | "block";
export type ViolationMode = "batch" | "live" | "precheck";

export type EventIn = {
  seq: number;
  kind?: EventKind;
  name: string;
  status?: string;
  input?: Record<string, any>;
  output?: Record<string, any>;
  attrs?: Record<string, any>;
  latency_ms?: number;
  timestamp?: string | null;
};

export type TracePolicyCreate = {
  source: string;
  action?: PolicyAction;
  agent_id?: string | null;
  environment?: string | null;
  enabled?: boolean;
};

export type TracePolicyUpdate = {
  source?: string | null;
  action?: PolicyAction | null;
  agent_id?: string | null;
  environment?: string | null;
  enabled?: boolean | null;
};

export type TracePolicy = {
  id: string;
  org_id: string;
  name: string;
  source: string;
  severity: PolicySeverity;
  action: PolicyAction;
  agent_id: string | null;
  environment: string | null;
  enabled: boolean;
  version: number;
  created_at: string;
  updated_at: string;
  archived_at: string | null;
};

export type PolicyValidateIn = { source: string };

export type PolicyError = { line: number; col: number; message: string };

export type PolicyValidateOut = { ok: boolean; errors: PolicyError[] };

export type Violation = {
  id: string | null;
  trace_id: string | null;
  agent_id: string;
  policy_id: string;
  policy_name: string;
  policy_version: number;
  severity: PolicySeverity;
  at_seq: number;
  counterexample: number[];
  message: string;
  mode: ViolationMode;
  detected_at: string;
};

export type PolicyCheckIn = {
  agent_id: string;
  trace_uid?: string | null;
  candidate: EventIn;
};

export type PolicyCheckOut = { allowed: boolean; violations: Violation[] };

export type EventsAppendIn = {
  agent_id: string;
  events: EventIn[];
  final?: boolean;
};

export type EventsAppendOut = { accepted: number; violations: Violation[] };

export type WsEventsMessage = EventsAppendIn & { type: "events"; trace_uid: string };

export type ViolationMessage = { type: "violation"; data: Violation };
