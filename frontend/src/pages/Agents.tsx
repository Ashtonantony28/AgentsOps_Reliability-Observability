import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { agentsApi } from "@/api/agents";
import StatusBadge from "@/components/common/StatusBadge";
import Loading from "@/components/common/Loading";
import EmptyState from "@/components/common/EmptyState";
import { errorMessage } from "@/api/client";
import type { Agent } from "@/types/agent";

export default function Agents() {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({ queryKey: ["agents"], queryFn: agentsApi.list });
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState({
    name: "",
    description: "",
    model_provider: "anthropic",
    model_name: "claude-opus-4-7",
    environment: "dev" as Agent["environment"],
    eval_threshold: 0.8,
  });
  const [error, setError] = useState<string | null>(null);
  const create = useMutation({
    mutationFn: () => agentsApi.create(form),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["agents"] });
      setOpen(false);
      setForm({ ...form, name: "", description: "" });
    },
    onError: (e: any) => setError(errorMessage(e)),
  });

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">Agents</h1>
          <p className="text-xs text-ink-400">Register and govern AI agents in your org.</p>
        </div>
        <button
          onClick={() => setOpen((x) => !x)}
          className="bg-brand-600 hover:bg-brand-500 text-white text-sm px-3 py-1.5 rounded"
        >
          + New agent
        </button>
      </div>

      {open && (
        <div className="bg-ink-900 border border-ink-800 rounded-lg p-4 grid grid-cols-1 md:grid-cols-3 gap-3 text-sm">
          <input
            className="bg-ink-950 border border-ink-700 rounded px-2 py-1"
            placeholder="Name"
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
          />
          <input
            className="bg-ink-950 border border-ink-700 rounded px-2 py-1 md:col-span-2"
            placeholder="Description"
            value={form.description}
            onChange={(e) => setForm({ ...form, description: e.target.value })}
          />
          <select
            className="bg-ink-950 border border-ink-700 rounded px-2 py-1"
            value={form.model_provider}
            onChange={(e) => setForm({ ...form, model_provider: e.target.value })}
          >
            <option value="anthropic">Anthropic</option>
            <option value="openai">OpenAI</option>
            <option value="cohere">Cohere</option>
            <option value="azure">Azure OpenAI</option>
          </select>
          <input
            className="bg-ink-950 border border-ink-700 rounded px-2 py-1"
            placeholder="Model"
            value={form.model_name}
            onChange={(e) => setForm({ ...form, model_name: e.target.value })}
          />
          <select
            className="bg-ink-950 border border-ink-700 rounded px-2 py-1"
            value={form.environment}
            onChange={(e) => setForm({ ...form, environment: e.target.value as Agent["environment"] })}
          >
            <option value="dev">dev</option>
            <option value="staging">staging</option>
            <option value="prod">prod</option>
          </select>
          <button
            onClick={() => create.mutate()}
            disabled={!form.name || create.isPending}
            className="md:col-span-3 bg-brand-600 disabled:opacity-50 hover:bg-brand-500 text-white py-1.5 rounded"
          >
            Create
          </button>
          {error && <div className="md:col-span-3 text-rose-300 text-xs">{error}</div>}
        </div>
      )}

      {isLoading ? (
        <Loading />
      ) : !data?.length ? (
        <EmptyState title="No agents yet" hint="Register your first agent above to start ingesting traces." />
      ) : (
        <div className="overflow-x-auto bg-ink-900 border border-ink-800 rounded-lg">
          <table className="w-full text-sm">
            <thead className="text-left text-xs uppercase text-ink-400 border-b border-ink-800">
              <tr>
                <th className="px-4 py-2">Name</th>
                <th className="px-4 py-2">Provider</th>
                <th className="px-4 py-2">Env</th>
                <th className="px-4 py-2">Health</th>
                <th className="px-4 py-2">Err 1h</th>
                <th className="px-4 py-2">p95 24h</th>
                <th className="px-4 py-2">Cost 24h</th>
                <th className="px-4 py-2">Version</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-ink-800">
              {data.map((a) => (
                <tr key={a.id} className="hover:bg-ink-900/50">
                  <td className="px-4 py-2">
                    <Link to={`/agents/${a.id}`} className="text-brand-300 hover:underline">
                      {a.name}
                    </Link>
                    <div className="text-xs text-ink-400">{a.description}</div>
                  </td>
                  <td className="px-4 py-2 text-ink-300">{a.model_provider}/{a.model_name}</td>
                  <td className="px-4 py-2"><StatusBadge status={a.environment} /></td>
                  <td className="px-4 py-2"><StatusBadge status={a.health} /></td>
                  <td className="px-4 py-2 text-ink-300">{(a.error_rate * 100).toFixed(1)}%</td>
                  <td className="px-4 py-2 text-ink-300">{a.p95_latency_ms}ms</td>
                  <td className="px-4 py-2 text-ink-300">${a.cost_24h.toFixed(2)}</td>
                  <td className="px-4 py-2 text-ink-300">{a.current_version}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
