import { useParams } from "react-router-dom";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { agentsApi } from "@/api/agents";
import { tracesApi } from "@/api/traces";
import StatusBadge from "@/components/common/StatusBadge";
import MetricCard from "@/components/common/MetricCard";
import MetricTimeSeries from "@/components/charts/MetricTimeSeries";
import TraceList from "@/components/traces/TraceList";
import Loading from "@/components/common/Loading";
import { errorMessage } from "@/api/client";

export default function AgentDetail() {
  const qc = useQueryClient();
  const { id } = useParams<{ id: string }>();
  const [hours, setHours] = useState(24);
  const agent = useQuery({ queryKey: ["agent", id], queryFn: () => agentsApi.get(id!) });
  const metrics = useQuery({
    queryKey: ["agent-metrics", id, hours],
    queryFn: () => tracesApi.metrics({ agent_id: id, hours }),
  });
  const traces = useQuery({
    queryKey: ["agent-traces", id],
    queryFn: () => tracesApi.list({ agent_id: id, limit: 50, since_hours: hours }),
  });
  const versions = useQuery({
    queryKey: ["agent-versions", id],
    queryFn: () => agentsApi.versions(id!),
  });

  const [promoteTarget, setPromoteTarget] = useState("prod");
  const [promoteVersion, setPromoteVersion] = useState("");
  const [force, setForce] = useState(false);
  const [promoteErr, setPromoteErr] = useState<string | null>(null);
  const promote = useMutation({
    mutationFn: () => agentsApi.promote(id!, { target_environment: promoteTarget, version: promoteVersion || agent.data!.current_version, force }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["agent", id] });
      qc.invalidateQueries({ queryKey: ["agent-versions", id] });
      setPromoteErr(null);
    },
    onError: (e: any) => setPromoteErr(errorMessage(e)),
  });

  if (agent.isLoading) return <Loading />;
  if (!agent.data) return <div className="text-sm text-rose-300">Agent not found.</div>;
  const a = agent.data;
  const totals = metrics.data?.totals || {};
  const errRate = (Number(totals.error_rate) || 0) * 100;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold flex items-center gap-2">
            {a.name} <StatusBadge status={a.environment} /> <StatusBadge status={a.health} />
          </h1>
          <p className="text-xs text-ink-400">{a.description}</p>
        </div>
        <div className="flex items-center gap-1 text-xs">
          {[1, 6, 24, 168, 720].map((h) => (
            <button
              key={h}
              onClick={() => setHours(h)}
              className={`px-2 py-1 rounded border ${
                hours === h ? "border-brand-500 text-brand-300 bg-brand-500/10" : "border-ink-700"
              }`}
            >
              {h < 24 ? `${h}h` : `${h / 24}d`}
            </button>
          ))}
        </div>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-5 gap-4">
        <MetricCard label="Traces" value={Number(totals.traces || 0).toLocaleString()} />
        <MetricCard label="Errors" value={Number(totals.errors || 0).toLocaleString()} tone={errRate > 5 ? "bad" : errRate > 2 ? "warn" : "good"} />
        <MetricCard label="Error rate" value={`${errRate.toFixed(2)}%`} tone={errRate > 5 ? "bad" : errRate > 2 ? "warn" : "good"} />
        <MetricCard label="p95 latency" value={`${a.p95_latency_ms}ms`} />
        <MetricCard label="Cost 24h" value={`$${a.cost_24h.toFixed(2)}`} />
      </div>

      {metrics.data && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <MetricTimeSeries data={metrics.data.error_rate} label="Error rate" color="#ef4444" formatY={(v) => `${(v * 100).toFixed(1)}%`} />
          <MetricTimeSeries data={metrics.data.latency_p95_ms} label="p95 latency" color="#7c5cff" formatY={(v) => `${Math.round(v)}ms`} />
          <MetricTimeSeries data={metrics.data.tokens} label="Tokens" color="#22c55e" />
          <MetricTimeSeries data={metrics.data.cost_usd} label="Cost (USD)" color="#f59e0b" formatY={(v) => `$${v.toFixed(2)}`} />
        </div>
      )}

      <div className="bg-ink-900 border border-ink-800 rounded-lg p-4">
        <div className="text-sm text-ink-300 mb-3">Recent traces</div>
        {traces.isLoading ? <Loading /> : <TraceList traces={traces.data || []} />}
      </div>

      <div className="bg-ink-900 border border-ink-800 rounded-lg p-4">
        <div className="text-sm text-ink-300 mb-3">Versions & promotion</div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-xs text-ink-400 uppercase border-b border-ink-800">
              <tr>
                <th className="text-left py-2 px-2">Version</th>
                <th className="text-left py-2 px-2">Model</th>
                <th className="text-left py-2 px-2">Environment</th>
                <th className="text-left py-2 px-2">Created</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-ink-800">
              {(versions.data || []).map((v) => (
                <tr key={v.id}>
                  <td className="py-2 px-2 text-ink-200">{v.version}</td>
                  <td className="py-2 px-2 text-ink-300">{v.model_name}</td>
                  <td className="py-2 px-2"><StatusBadge status={v.environment} /></td>
                  <td className="py-2 px-2 text-ink-400">{new Date(v.created_at).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="mt-4 flex flex-wrap items-center gap-2 text-sm">
          <span className="text-ink-400">Promote</span>
          <input
            className="bg-ink-950 border border-ink-700 rounded px-2 py-1 w-24"
            placeholder="version"
            value={promoteVersion}
            onChange={(e) => setPromoteVersion(e.target.value)}
          />
          <span className="text-ink-400">→</span>
          <select className="bg-ink-950 border border-ink-700 rounded px-2 py-1" value={promoteTarget}
                  onChange={(e) => setPromoteTarget(e.target.value)}>
            <option value="staging">staging</option>
            <option value="prod">prod</option>
          </select>
          <label className="text-xs text-ink-300 flex items-center gap-1">
            <input type="checkbox" checked={force} onChange={(e) => setForce(e.target.checked)} /> force (skip eval gate)
          </label>
          <button
            onClick={() => promote.mutate()}
            disabled={promote.isPending}
            className="bg-brand-600 hover:bg-brand-500 text-white text-xs px-3 py-1 rounded disabled:opacity-50"
          >
            Promote
          </button>
          {promoteErr && <div className="text-xs text-rose-300">{promoteErr}</div>}
        </div>
      </div>
    </div>
  );
}
