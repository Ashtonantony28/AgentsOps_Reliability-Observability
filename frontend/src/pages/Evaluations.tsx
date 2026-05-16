import { Link } from "react-router-dom";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { evalsApi } from "@/api/evaluations";
import { agentsApi } from "@/api/agents";
import StatusBadge from "@/components/common/StatusBadge";
import Loading from "@/components/common/Loading";
import EmptyState from "@/components/common/EmptyState";
import { errorMessage } from "@/api/client";

export default function Evaluations() {
  const qc = useQueryClient();
  const sets = useQuery({ queryKey: ["eval-sets"], queryFn: evalsApi.listSets });
  const runs = useQuery({ queryKey: ["eval-runs"], queryFn: () => evalsApi.listRuns({}) });
  const agents = useQuery({ queryKey: ["agents"], queryFn: agentsApi.list });

  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState({ name: "", description: "", evaluators: "exact,semantic", cases: "" });
  const create = useMutation({
    mutationFn: () => {
      const evaluators = form.evaluators.split(",").map((s) => s.trim()).filter(Boolean);
      const cases = form.cases.split("\n").filter(Boolean).map((line) => {
        const [input, expected_output] = line.split("|").map((s) => s.trim());
        return { input, expected_output: expected_output || "" };
      });
      return evalsApi.createSet({ name: form.name, description: form.description, evaluators, cases });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["eval-sets"] });
      setCreating(false);
      setForm({ name: "", description: "", evaluators: "exact,semantic", cases: "" });
    },
  });

  const [runSetId, setRunSetId] = useState<string | null>(null);
  const [runAgentId, setRunAgentId] = useState<string>("");
  const [runErr, setRunErr] = useState<string | null>(null);
  const runIt = useMutation({
    mutationFn: () => evalsApi.runSet(runSetId!, { agent_id: runAgentId }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["eval-runs"] });
      setRunSetId(null);
      setRunErr(null);
    },
    onError: (e: any) => setRunErr(errorMessage(e)),
  });

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">Evaluations</h1>
          <p className="text-xs text-ink-400">Define eval sets, run them, and compare across versions.</p>
        </div>
        <button
          onClick={() => setCreating(!creating)}
          className="bg-brand-600 hover:bg-brand-500 text-white text-sm px-3 py-1.5 rounded"
        >
          + New set
        </button>
      </div>

      {creating && (
        <div className="bg-ink-900 border border-ink-800 rounded-lg p-4 space-y-2 text-sm">
          <input className="bg-ink-950 border border-ink-700 rounded px-2 py-1 w-full"
                 placeholder="Name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          <input className="bg-ink-950 border border-ink-700 rounded px-2 py-1 w-full"
                 placeholder="Description" value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} />
          <input className="bg-ink-950 border border-ink-700 rounded px-2 py-1 w-full"
                 placeholder="Evaluators (comma) e.g. exact,semantic,llm_judge,latency,cost"
                 value={form.evaluators} onChange={(e) => setForm({ ...form, evaluators: e.target.value })} />
          <textarea
            className="bg-ink-950 border border-ink-700 rounded px-2 py-1 w-full h-32"
            placeholder={"Cases (one per line, format: input | expected_output)"}
            value={form.cases}
            onChange={(e) => setForm({ ...form, cases: e.target.value })}
          />
          <button onClick={() => create.mutate()} disabled={!form.name || create.isPending}
                  className="bg-brand-600 hover:bg-brand-500 disabled:opacity-50 text-white px-3 py-1.5 rounded">
            Create set
          </button>
        </div>
      )}

      <section>
        <h2 className="text-sm uppercase text-ink-400 mb-2">Sets</h2>
        {sets.isLoading ? <Loading /> : !sets.data?.length ? (
          <EmptyState title="No evaluation sets" hint="Create your first set above." />
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            {sets.data.map((s) => (
              <div key={s.id} className="bg-ink-900 border border-ink-800 rounded p-4">
                <div className="flex items-center justify-between">
                  <Link to={`/evaluations/${s.id}`} className="font-medium text-ink-100 hover:text-brand-300">
                    {s.name}
                  </Link>
                  <span className="text-xs text-ink-400">{s.case_count} cases</span>
                </div>
                <div className="text-xs text-ink-400 mt-1">{s.description}</div>
                <div className="text-xs text-ink-500 mt-2">evaluators: {s.evaluators.join(", ")}</div>
                <button
                  onClick={() => setRunSetId(s.id)}
                  className="mt-3 text-xs bg-ink-800 hover:bg-ink-700 text-ink-100 px-2 py-1 rounded"
                >
                  Run…
                </button>
                {runSetId === s.id && (
                  <div className="mt-2 flex items-center gap-2 text-xs">
                    <select className="bg-ink-950 border border-ink-700 rounded px-1 py-0.5"
                            value={runAgentId} onChange={(e) => setRunAgentId(e.target.value)}>
                      <option value="">select agent…</option>
                      {(agents.data || []).map((a) => (<option key={a.id} value={a.id}>{a.name}</option>))}
                    </select>
                    <button
                      onClick={() => runIt.mutate()}
                      disabled={!runAgentId || runIt.isPending}
                      className="bg-brand-600 hover:bg-brand-500 disabled:opacity-50 text-white px-2 py-0.5 rounded"
                    >
                      Run
                    </button>
                    {runErr && <span className="text-rose-300">{runErr}</span>}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </section>

      <section>
        <h2 className="text-sm uppercase text-ink-400 mb-2">Recent runs</h2>
        <div className="overflow-x-auto bg-ink-900 border border-ink-800 rounded">
          <table className="w-full text-sm">
            <thead className="text-xs uppercase text-ink-400 border-b border-ink-800">
              <tr>
                <th className="text-left px-4 py-2">When</th>
                <th className="text-left px-4 py-2">Set</th>
                <th className="text-left px-4 py-2">Agent</th>
                <th className="text-left px-4 py-2">Status</th>
                <th className="text-left px-4 py-2">Score</th>
                <th className="text-left px-4 py-2">Pass/Fail</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-ink-800">
              {(runs.data || []).map((r) => (
                <tr key={r.id} className="hover:bg-ink-900/60">
                  <td className="px-4 py-2 text-ink-300">{new Date(r.created_at).toLocaleString()}</td>
                  <td className="px-4 py-2">
                    <Link to={`/evaluations/${r.set_id}`} className="text-brand-300 hover:underline">
                      {r.set_id.slice(0, 8)}
                    </Link>
                  </td>
                  <td className="px-4 py-2 text-ink-300">{r.agent_id.slice(0, 8)} ({r.agent_version})</td>
                  <td className="px-4 py-2"><StatusBadge status={r.status} /></td>
                  <td className="px-4 py-2 text-ink-200">{r.score.toFixed(2)}</td>
                  <td className="px-4 py-2 text-ink-300">{r.passed} / {r.failed}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
