import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { tracesApi } from "@/api/traces";
import { tracePoliciesApi } from "@/api/tracePolicies";
import { useWsStore } from "@/stores/wsStore";
import ViolationsPanel, { counterexamplePending, violationKey } from "@/components/traces/ViolationsPanel";
import StatusBadge from "@/components/common/StatusBadge";
import TraceSpanTree from "@/components/traces/TraceSpanTree";
import Loading from "@/components/common/Loading";

export default function TraceDetail() {
  const { id } = useParams<{ id: string }>();
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({ queryKey: ["trace", id], queryFn: () => tracesApi.get(id!) });
  const violations = useQuery({
    queryKey: ["trace-violations", id],
    queryFn: () => tracePoliciesApi.traceViolations(id!),
    refetchInterval: (q) =>
      q.state.data?.some(counterexamplePending) && q.state.dataUpdateCount < 60 ? 5000 : false,
  });
  const latestLive = useWsStore((s) => s.violations[0]);
  useEffect(() => {
    if (latestLive?.trace_id === id) {
      qc.invalidateQueries({ queryKey: ["trace-violations", id] });
      qc.invalidateQueries({ queryKey: ["trace", id] });
    }
  }, [latestLive, id, qc]);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const selected =
    violations.data?.find((v) => violationKey(v) === selectedKey) ?? violations.data?.[0] ?? null;

  if (isLoading) return <Loading />;
  if (!data) return <div className="text-sm text-rose-300">Trace not found.</div>;
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold flex items-center gap-2">
          Trace <span className="text-ink-400 text-sm">#{data.trace_uid.slice(0, 8)}</span>
          <StatusBadge status={data.status} />
          <StatusBadge status={data.environment} />
        </h1>
        <p className="text-xs text-ink-400 mt-1">
          {new Date(data.created_at).toLocaleString()} · {data.latency_ms} ms · ${data.cost_usd.toFixed(4)}
        </p>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="bg-ink-900 border border-ink-800 rounded p-4">
          <div className="text-xs text-ink-400 mb-1">Input</div>
          <pre className="text-sm text-ink-100 whitespace-pre-wrap break-words">{data.input}</pre>
        </div>
        <div className="bg-ink-900 border border-ink-800 rounded p-4">
          <div className="text-xs text-ink-400 mb-1">Output</div>
          <pre className="text-sm text-ink-100 whitespace-pre-wrap break-words">
            {data.error_message ? `[ERROR] ${data.error_message}\n\n${data.output}` : data.output}
          </pre>
        </div>
      </div>

      <div className="bg-ink-900 border border-ink-800 rounded p-4">
        <div className="text-sm text-ink-300 mb-2">Policy violations</div>
        {violations.isLoading ? <Loading /> : violations.isError ? (
          <div className="text-sm text-ink-400">Violations unavailable.</div>
        ) : (
          <ViolationsPanel
            violations={violations.data || []}
            selected={selected}
            onSelect={(v) => setSelectedKey(violationKey(v))}
          />
        )}
      </div>

      <div className="bg-ink-900 border border-ink-800 rounded p-4">
        <div className="text-sm text-ink-300 mb-2">Spans / tool calls</div>
        <TraceSpanTree spans={data.spans || []} counterexample={selected?.counterexample} atSeq={selected?.at_seq} />
      </div>

      <div className="bg-ink-900 border border-ink-800 rounded p-4">
        <div className="text-sm text-ink-300 mb-2">Telemetry</div>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
          <div><div className="text-xs text-ink-400">Tokens in</div>{data.tokens_input}</div>
          <div><div className="text-xs text-ink-400">Tokens out</div>{data.tokens_output}</div>
          <div><div className="text-xs text-ink-400">PII flags</div>{(data.pii_flags || []).join(", ") || "—"}</div>
          <div><div className="text-xs text-ink-400">Injection score</div>{data.injection_score.toFixed(3)}</div>
        </div>
        {Object.keys(data.meta || {}).length > 0 && (
          <pre className="mt-3 text-xs text-ink-300 bg-ink-950 p-2 rounded whitespace-pre-wrap">
{JSON.stringify(data.meta, null, 2)}
          </pre>
        )}
      </div>
    </div>
  );
}
