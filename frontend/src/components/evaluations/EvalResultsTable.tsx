import { EvalResult } from "@/types/evaluation";
import StatusBadge from "@/components/common/StatusBadge";

export default function EvalResultsTable({ results }: { results: EvalResult[] }) {
  if (!results?.length) return <div className="text-ink-400 text-sm">No results yet.</div>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="text-left text-xs uppercase text-ink-400 border-b border-ink-800">
          <tr>
            <th className="py-2 pr-2">Status</th>
            <th className="py-2 pr-2">Score</th>
            <th className="py-2 pr-2">By evaluator</th>
            <th className="py-2 pr-2">Actual output</th>
            <th className="py-2 pr-2">Reason</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-ink-800">
          {results.map((r) => (
            <tr key={r.id}>
              <td className="py-2 pr-2">
                <StatusBadge status={r.passed ? "success" : "failed"} />
              </td>
              <td className="py-2 pr-2 text-ink-200">{r.score.toFixed(2)}</td>
              <td className="py-2 pr-2 text-xs text-ink-300">
                {Object.entries(r.scores_by_evaluator || {}).map(([k, v]) => (
                  <span key={k} className="mr-2">
                    {k}:{Number(v).toFixed(2)}
                  </span>
                ))}
              </td>
              <td className="py-2 pr-2 text-ink-200 max-w-md truncate">{r.actual_output}</td>
              <td className="py-2 pr-2 text-xs text-ink-400">{r.reason}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
