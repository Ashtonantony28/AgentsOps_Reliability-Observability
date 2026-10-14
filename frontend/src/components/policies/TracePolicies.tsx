import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { errorMessage } from "@/api/client";
import { tracePoliciesApi } from "@/api/tracePolicies";
import StatusBadge from "@/components/common/StatusBadge";
import Loading from "@/components/common/Loading";
import PolicyEditor from "./PolicyEditor";

export default function TracePolicies() {
  const qc = useQueryClient();
  const policies = useQuery({ queryKey: ["trace-policies"], queryFn: tracePoliciesApi.list });
  const [editing, setEditing] = useState<string | "new" | null>(null);
  const done = () => {
    setEditing(null);
    qc.invalidateQueries({ queryKey: ["trace-policies"] });
  };
  const toggle = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) => tracePoliciesApi.update(id, { enabled }),
    onSettled: () => qc.invalidateQueries({ queryKey: ["trace-policies"] }),
  });
  const remove = useMutation({
    mutationFn: (id: string) => tracePoliciesApi.remove(id),
    onSettled: () => qc.invalidateQueries({ queryKey: ["trace-policies"] }),
  });
  const mutationError = toggle.error || remove.error;
  const run = <T,>(m: { mutate: (x: T) => void }, other: { reset: () => void }, x: T) => {
    other.reset();
    m.mutate(x);
  };

  return (
    <section className="bg-ink-900 border border-ink-800 rounded p-4">
      <div className="flex items-center justify-between mb-2">
        <div>
          <h2 className="text-sm uppercase text-ink-400">Trace policies</h2>
          <p className="text-xs text-ink-500">Temporal rules checked against every event in a trace.</p>
        </div>
        {editing === null && (
          <button className="text-xs px-3 py-1.5 rounded bg-brand-600 hover:bg-brand-500 text-white"
                  onClick={() => setEditing("new")}>
            New policy
          </button>
        )}
      </div>
      {editing === "new" && (
        <div className="mb-3 pb-3 border-b border-ink-800"><PolicyEditor onDone={done} /></div>
      )}
      {mutationError && <div className="text-xs text-rose-300 mb-2">{errorMessage(mutationError)}</div>}
      {policies.isLoading ? <Loading /> : (
        <ul className="divide-y divide-ink-800 text-sm">
          {(policies.data || []).map((p) =>
            editing === p.id ? (
              <li key={p.id} className="py-2"><PolicyEditor policy={p} onDone={done} /></li>
            ) : (
              <li key={p.id} className="py-2 flex items-start justify-between gap-4">
                <div className="min-w-0">
                  <div className="font-medium flex items-center gap-2">
                    {p.name}
                    <StatusBadge status={p.severity} />
                    <StatusBadge status={p.action} />
                    <span className="text-xs text-ink-500">v{p.version}</span>
                  </div>
                  <div className="text-xs text-ink-400">
                    {p.agent_id ? `agent ${p.agent_id.slice(0, 8)}` : "all agents"} · {p.environment || "any environment"}
                  </div>
                  <pre className="text-[11px] text-ink-400 mt-1 font-mono whitespace-pre-wrap">{p.source.trim()}</pre>
                </div>
                <div className="flex items-center gap-3 shrink-0 text-xs">
                  <label className="flex items-center gap-1 text-ink-300">
                    <input type="checkbox" checked={p.enabled}
                           onChange={(e) => run(toggle, remove, { id: p.id, enabled: e.target.checked })} /> enabled
                  </label>
                  <button className="text-ink-200 hover:underline" onClick={() => setEditing(p.id)}>edit</button>
                  <button className="text-rose-300 hover:underline" onClick={() => confirm(`Delete policy ${p.name}?`) && run(remove, toggle, p.id)}>delete</button>
                </div>
              </li>
            )
          )}
          {policies.data?.length === 0 && editing !== "new" && (
            <li className="py-2 text-xs text-ink-400">No trace policies yet.</li>
          )}
        </ul>
      )}
    </section>
  );
}
