import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { tracesApi } from "@/api/traces";
import { agentsApi } from "@/api/agents";
import TraceList from "@/components/traces/TraceList";
import LiveTraceFeed from "@/components/traces/LiveTraceFeed";
import Loading from "@/components/common/Loading";
import { API_URL } from "@/api/client";

export default function Traces() {
  const [filters, setFilters] = useState({ agent_id: "", status: "", environment: "", hours: 24 });
  const agents = useQuery({ queryKey: ["agents"], queryFn: agentsApi.list });
  const traces = useQuery({
    queryKey: ["traces", filters],
    queryFn: () =>
      tracesApi.list({
        agent_id: filters.agent_id || undefined,
        status: filters.status || undefined,
        environment: filters.environment || undefined,
        limit: 200,
        since_hours: filters.hours,
      }),
  });

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-xl font-semibold">Trace explorer</h1>
          <p className="text-xs text-ink-400">Search, filter, and drill into agent executions.</p>
        </div>
        <a
          href={`${API_URL}/api/traces/export.csv?hours=${filters.hours}`}
          className="text-xs px-3 py-1.5 rounded border border-ink-700 text-ink-200 hover:border-ink-500"
        >
          Export CSV
        </a>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-5 gap-2 text-sm">
        <select
          className="bg-ink-950 border border-ink-700 rounded px-2 py-1"
          value={filters.agent_id}
          onChange={(e) => setFilters({ ...filters, agent_id: e.target.value })}
        >
          <option value="">All agents</option>
          {(agents.data || []).map((a) => (
            <option key={a.id} value={a.id}>{a.name}</option>
          ))}
        </select>
        <select
          className="bg-ink-950 border border-ink-700 rounded px-2 py-1"
          value={filters.status}
          onChange={(e) => setFilters({ ...filters, status: e.target.value })}
        >
          <option value="">Any status</option>
          <option value="success">success</option>
          <option value="error">error</option>
          <option value="timeout">timeout</option>
        </select>
        <select
          className="bg-ink-950 border border-ink-700 rounded px-2 py-1"
          value={filters.environment}
          onChange={(e) => setFilters({ ...filters, environment: e.target.value })}
        >
          <option value="">Any env</option>
          <option value="dev">dev</option>
          <option value="staging">staging</option>
          <option value="prod">prod</option>
        </select>
        <select
          className="bg-ink-950 border border-ink-700 rounded px-2 py-1"
          value={filters.hours}
          onChange={(e) => setFilters({ ...filters, hours: Number(e.target.value) })}
        >
          <option value="1">last 1h</option>
          <option value="6">last 6h</option>
          <option value="24">last 24h</option>
          <option value="168">last 7d</option>
          <option value="720">last 30d</option>
        </select>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div className="lg:col-span-2 bg-ink-900 border border-ink-800 rounded-lg p-4">
          {traces.isLoading ? <Loading /> : <TraceList traces={traces.data || []} />}
        </div>
        <LiveTraceFeed />
      </div>
    </div>
  );
}
