import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { tracesApi } from "@/api/traces";
import { agentsApi } from "@/api/agents";
import { incidentsApi } from "@/api/alerts";
import MetricCard from "@/components/common/MetricCard";
import MetricTimeSeries from "@/components/charts/MetricTimeSeries";
import CostBurnChart from "@/components/charts/CostBurnChart";
import LiveTraceFeed from "@/components/traces/LiveTraceFeed";
import StatusBadge from "@/components/common/StatusBadge";
import Loading from "@/components/common/Loading";
import AlertBanner from "@/components/common/AlertBanner";

const RANGES = [
  { label: "1h", hours: 1 },
  { label: "6h", hours: 6 },
  { label: "24h", hours: 24 },
  { label: "7d", hours: 24 * 7 },
  { label: "30d", hours: 24 * 30 },
];

export default function Dashboard() {
  const [hours, setHours] = useState(24);
  const metrics = useQuery({ queryKey: ["metrics", hours], queryFn: () => tracesApi.metrics({ hours }) });
  const cost = useQuery({ queryKey: ["cost-burn"], queryFn: () => tracesApi.costBurn({ days: 30 }) });
  const agents = useQuery({ queryKey: ["agents"], queryFn: agentsApi.list });
  const incidents = useQuery({
    queryKey: ["incidents", "open"],
    queryFn: () => incidentsApi.list("open"),
  });

  const totals = metrics.data?.totals || {};
  const errRate = (Number(totals.error_rate) || 0) * 100;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">Overview</h1>
          <p className="text-xs text-ink-400">Cross-agent reliability & cost — last {hours}h</p>
        </div>
        <div className="flex items-center gap-1 text-xs">
          {RANGES.map((r) => (
            <button
              key={r.label}
              onClick={() => setHours(r.hours)}
              className={`px-2 py-1 rounded border ${
                hours === r.hours
                  ? "border-brand-500 text-brand-300 bg-brand-500/10"
                  : "border-ink-700 text-ink-300 hover:text-ink-100"
              }`}
            >
              {r.label}
            </button>
          ))}
        </div>
      </div>

      {incidents.data && incidents.data.length > 0 && (
        <AlertBanner tone="error" title={`${incidents.data.length} open incident(s)`}>
          {incidents.data
            .slice(0, 3)
            .map((i) => i.title)
            .join(" · ")}
        </AlertBanner>
      )}

      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <MetricCard label="Traces" value={Number(totals.traces || 0).toLocaleString()} hint={`last ${hours}h`} />
        <MetricCard
          label="Error rate"
          value={`${errRate.toFixed(2)}%`}
          tone={errRate > 5 ? "bad" : errRate > 2 ? "warn" : "good"}
        />
        <MetricCard label="Tokens" value={Number(totals.tokens || 0).toLocaleString()} />
        <MetricCard label="Cost" value={`$${Number(totals.cost_usd || 0).toFixed(2)}`} />
      </div>

      {metrics.isLoading ? (
        <Loading />
      ) : metrics.data ? (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <MetricTimeSeries data={metrics.data.success_rate} label="Success rate" color="#22c55e" formatY={(v) => `${(v * 100).toFixed(1)}%`} />
          <MetricTimeSeries data={metrics.data.error_rate} label="Error rate" color="#ef4444" formatY={(v) => `${(v * 100).toFixed(1)}%`} />
          <MetricTimeSeries data={metrics.data.latency_p95_ms} label="Latency p95 (ms)" color="#7c5cff" formatY={(v) => `${Math.round(v)}ms`} />
          <MetricTimeSeries data={metrics.data.cost_usd} label="Cost (USD)" color="#f59e0b" formatY={(v) => `$${v.toFixed(2)}`} />
        </div>
      ) : null}

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div className="lg:col-span-2">
          {cost.data && <CostBurnChart data={cost.data} />}
        </div>
        <LiveTraceFeed />
      </div>

      <div className="bg-ink-900 border border-ink-800 rounded-lg p-4">
        <div className="text-sm text-ink-300 mb-3">Agent health</div>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
          {(agents.data || []).map((a) => (
            <div key={a.id} className="border border-ink-800 rounded p-3 hover:border-ink-600">
              <div className="flex items-center justify-between">
                <div className="font-medium text-sm">{a.name}</div>
                <StatusBadge status={a.health} />
              </div>
              <div className="text-xs text-ink-400 mt-1">{a.model_provider} · {a.model_name}</div>
              <div className="text-xs text-ink-300 mt-2 flex justify-between">
                <span>err {(a.error_rate * 100).toFixed(1)}%</span>
                <span>p95 {a.p95_latency_ms}ms</span>
                <span>${a.cost_24h.toFixed(2)}/d</span>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
