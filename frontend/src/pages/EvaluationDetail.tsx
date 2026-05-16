import { useParams } from "react-router-dom";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { evalsApi } from "@/api/evaluations";
import EvalResultsTable from "@/components/evaluations/EvalResultsTable";
import EvalDiffView from "@/components/evaluations/EvalDiffView";
import Loading from "@/components/common/Loading";
import StatusBadge from "@/components/common/StatusBadge";

export default function EvaluationDetail() {
  const { id } = useParams<{ id: string }>();
  const set = useQuery({ queryKey: ["eval-set", id], queryFn: () => evalsApi.getSet(id!) });
  const runs = useQuery({ queryKey: ["eval-set-runs", id], queryFn: () => evalsApi.listRuns({ set_id: id }) });
  const [selRun, setSelRun] = useState<string | null>(null);
  const [diffA, setDiffA] = useState<string | null>(null);
  const [diffB, setDiffB] = useState<string | null>(null);
  const runDetail = useQuery({
    queryKey: ["eval-run", selRun],
    queryFn: () => evalsApi.getRun(selRun!),
    enabled: !!selRun,
  });
  const diff = useQuery({
    queryKey: ["eval-diff", diffA, diffB],
    queryFn: () => evalsApi.diff(diffA!, diffB!),
    enabled: !!(diffA && diffB),
  });

  if (set.isLoading) return <Loading />;
  if (!set.data) return <div className="text-rose-300">Not found</div>;
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">{set.data.name}</h1>
        <p className="text-xs text-ink-400">{set.data.description}</p>
        <p className="text-xs text-ink-500 mt-1">Evaluators: {set.data.evaluators.join(", ")}</p>
      </div>

      <div className="bg-ink-900 border border-ink-800 rounded p-4">
        <h2 className="text-sm text-ink-300 mb-2">Cases ({set.data.case_count})</h2>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-xs uppercase text-ink-400 border-b border-ink-800">
              <tr>
                <th className="text-left py-2 px-2">Input</th>
                <th className="text-left py-2 px-2">Expected</th>
                <th className="text-left py-2 px-2">Weight</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-ink-800">
              {(set.data.cases || []).map((c) => (
                <tr key={c.id}>
                  <td className="py-2 px-2 text-ink-100 max-w-md truncate">{c.input}</td>
                  <td className="py-2 px-2 text-ink-300 max-w-md truncate">{c.expected_output}</td>
                  <td className="py-2 px-2 text-ink-300">{c.weight}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="bg-ink-900 border border-ink-800 rounded p-4">
        <div className="flex items-center justify-between mb-2">
          <h2 className="text-sm text-ink-300">Runs</h2>
          {diffA && diffB && (
            <button onClick={() => { setDiffA(null); setDiffB(null); }}
                    className="text-xs text-ink-400 hover:text-ink-200">clear diff</button>
          )}
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-xs uppercase text-ink-400 border-b border-ink-800">
              <tr>
                <th className="text-left py-2 px-2">Diff</th>
                <th className="text-left py-2 px-2">When</th>
                <th className="text-left py-2 px-2">Agent</th>
                <th className="text-left py-2 px-2">Status</th>
                <th className="text-left py-2 px-2">Score</th>
                <th className="text-left py-2 px-2">Pass/Fail</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-ink-800">
              {(runs.data || []).map((r) => (
                <tr key={r.id} className={selRun === r.id ? "bg-brand-500/5" : "hover:bg-ink-900/60"}>
                  <td className="py-2 px-2 text-xs">
                    <button
                      onClick={() => { if (!diffA) setDiffA(r.id); else if (!diffB) setDiffB(r.id); else { setDiffA(r.id); setDiffB(null); } }}
                      className={`px-1 rounded ${diffA === r.id || diffB === r.id ? "bg-brand-600 text-white" : "bg-ink-800 hover:bg-ink-700"}`}
                    >
                      {diffA === r.id ? "A" : diffB === r.id ? "B" : "diff"}
                    </button>
                  </td>
                  <td className="py-2 px-2 text-ink-300">
                    <button className="text-brand-300 hover:underline" onClick={() => setSelRun(r.id)}>
                      {new Date(r.created_at).toLocaleString()}
                    </button>
                  </td>
                  <td className="py-2 px-2 text-ink-300">{r.agent_id.slice(0, 8)} ({r.agent_version})</td>
                  <td className="py-2 px-2"><StatusBadge status={r.status} /></td>
                  <td className="py-2 px-2 text-ink-200">{r.score.toFixed(2)}</td>
                  <td className="py-2 px-2 text-ink-300">{r.passed} / {r.failed}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {diff.data && <EvalDiffView diff={diff.data} />}

      {selRun && runDetail.data && (
        <div className="bg-ink-900 border border-ink-800 rounded p-4">
          <h2 className="text-sm text-ink-300 mb-2">Run results — {runDetail.data.id.slice(0, 8)}</h2>
          <EvalResultsTable results={runDetail.data.results} />
        </div>
      )}
    </div>
  );
}
