import { useQuery } from "@tanstack/react-query";
import { reportsApi } from "@/api/auth";
import { API_URL } from "@/api/client";
import Loading from "@/components/common/Loading";
import MetricCard from "@/components/common/MetricCard";

export default function Reports() {
  const summary = useQuery({ queryKey: ["report-summary"], queryFn: () => reportsApi.summary(7) });
  if (summary.isLoading) return <Loading />;
  const s: any = summary.data || {};
  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">Reports</h1>
          <p className="text-xs text-ink-400">Executive summary and exports.</p>
        </div>
        <div className="flex gap-2">
          <a className="text-xs px-3 py-1.5 rounded border border-ink-700 text-ink-200"
             href={`${API_URL}/api/reports/summary.pdf?days=7`}>Download PDF (7d)</a>
          <a className="text-xs px-3 py-1.5 rounded border border-ink-700 text-ink-200"
             href={`${API_URL}/api/reports/traces.csv?hours=168`}>Traces CSV (7d)</a>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div className="bg-ink-900 border border-ink-800 rounded p-4">
          <h2 className="text-sm uppercase text-ink-400 mb-2">Top by cost (7d)</h2>
          <ul className="text-sm divide-y divide-ink-800">
            {(s.top_by_cost || []).map((a: any) => (
              <li key={a.agent_id} className="flex justify-between py-1">
                <span>{a.agent_name}</span>
                <span className="text-ink-300">${a.cost_usd.toFixed(2)}</span>
              </li>
            ))}
          </ul>
        </div>
        <div className="bg-ink-900 border border-ink-800 rounded p-4">
          <h2 className="text-sm uppercase text-ink-400 mb-2">Worst by error rate (7d)</h2>
          <ul className="text-sm divide-y divide-ink-800">
            {(s.worst_by_error_rate || []).map((a: any) => (
              <li key={a.agent_id} className="flex justify-between py-1">
                <span>{a.agent_name}</span>
                <span className="text-rose-300">{(a.error_rate * 100).toFixed(1)}%</span>
              </li>
            ))}
          </ul>
        </div>
      </div>

      <div className="bg-ink-900 border border-ink-800 rounded p-4">
        <h2 className="text-sm uppercase text-ink-400 mb-2">All agents (7d)</h2>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-xs uppercase text-ink-400 border-b border-ink-800">
              <tr>
                <th className="text-left py-2 px-2">Agent</th>
                <th className="text-left py-2 px-2">Traces</th>
                <th className="text-left py-2 px-2">Errors</th>
                <th className="text-left py-2 px-2">Error rate</th>
                <th className="text-left py-2 px-2">Cost</th>
                <th className="text-left py-2 px-2">Tokens</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-ink-800">
              {(s.agents || []).map((a: any) => (
                <tr key={a.agent_id}>
                  <td className="py-2 px-2">{a.agent_name}</td>
                  <td className="py-2 px-2 text-ink-300">{a.traces.toLocaleString()}</td>
                  <td className="py-2 px-2 text-ink-300">{a.errors.toLocaleString()}</td>
                  <td className="py-2 px-2 text-ink-300">{(a.error_rate * 100).toFixed(1)}%</td>
                  <td className="py-2 px-2 text-ink-300">${a.cost_usd.toFixed(2)}</td>
                  <td className="py-2 px-2 text-ink-300">{a.tokens.toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
