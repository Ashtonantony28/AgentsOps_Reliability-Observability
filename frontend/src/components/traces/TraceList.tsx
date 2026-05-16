import { Trace } from "@/types/trace";
import StatusBadge from "@/components/common/StatusBadge";
import { format } from "date-fns";
import { Link } from "react-router-dom";

export default function TraceList({ traces }: { traces: Trace[] }) {
  if (!traces?.length) {
    return <div className="text-ink-400 text-sm p-4">No traces.</div>;
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="text-left text-xs uppercase tracking-wide text-ink-400 border-b border-ink-800">
          <tr>
            <th className="py-2 pr-2">Time</th>
            <th className="py-2 pr-2">Status</th>
            <th className="py-2 pr-2">Latency</th>
            <th className="py-2 pr-2">Tokens</th>
            <th className="py-2 pr-2">Cost</th>
            <th className="py-2 pr-2">Env</th>
            <th className="py-2 pr-2">Flags</th>
            <th className="py-2 pr-2">Input</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-ink-800">
          {traces.map((t) => (
            <tr key={t.id} className="hover:bg-ink-900/60">
              <td className="py-2 pr-2 text-ink-300 whitespace-nowrap">
                <Link to={`/traces/${t.id}`} className="text-brand-400 hover:underline">
                  {format(new Date(t.created_at), "MMM d HH:mm:ss")}
                </Link>
              </td>
              <td className="py-2 pr-2"><StatusBadge status={t.status} /></td>
              <td className="py-2 pr-2 text-ink-300">{t.latency_ms} ms</td>
              <td className="py-2 pr-2 text-ink-300">{(t.tokens_input + t.tokens_output).toLocaleString()}</td>
              <td className="py-2 pr-2 text-ink-300">${t.cost_usd.toFixed(4)}</td>
              <td className="py-2 pr-2"><StatusBadge status={t.environment} /></td>
              <td className="py-2 pr-2 text-xs text-ink-400 space-x-1">
                {t.pii_flags?.map((p) => (
                  <span key={p} className="px-1 rounded bg-amber-500/20 text-amber-300">{p}</span>
                ))}
                {t.injection_score > 0.3 && (
                  <span className="px-1 rounded bg-rose-500/20 text-rose-300">inj</span>
                )}
              </td>
              <td className="py-2 pr-2 text-ink-200 max-w-md truncate">{t.input}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
