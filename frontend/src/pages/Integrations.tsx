import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { integrationsApi } from "@/api/auth";
import StatusBadge from "@/components/common/StatusBadge";
import Loading from "@/components/common/Loading";

const KINDS = [
  { kind: "openai", label: "OpenAI", fields: ["api_key"] },
  { kind: "anthropic", label: "Anthropic", fields: ["api_key"] },
  { kind: "cohere", label: "Cohere", fields: ["api_key"] },
  { kind: "azure", label: "Azure OpenAI", fields: ["api_key", "endpoint"] },
  { kind: "slack", label: "Slack", fields: ["webhook_url"] },
  { kind: "pagerduty", label: "PagerDuty", fields: ["routing_key"] },
  { kind: "msteams", label: "Microsoft Teams", fields: ["webhook_url"] },
  { kind: "datadog", label: "Datadog", fields: ["api_key"] },
  { kind: "grafana", label: "Grafana", fields: ["api_key", "url"] },
  { kind: "newrelic", label: "New Relic", fields: ["api_key"] },
  { kind: "github_actions", label: "GitHub Actions", fields: ["token", "repo"] },
  { kind: "gitlab_ci", label: "GitLab CI", fields: ["token", "project"] },
  { kind: "webhook", label: "Generic webhook", fields: ["url"] },
];

export default function Integrations() {
  const qc = useQueryClient();
  const list = useQuery({ queryKey: ["integrations"], queryFn: integrationsApi.list });
  const [selKind, setSelKind] = useState("slack");
  const [name, setName] = useState("");
  const [config, setConfig] = useState<Record<string, string>>({});
  const kindDef = KINDS.find((k) => k.kind === selKind)!;
  const create = useMutation({
    mutationFn: () => integrationsApi.create({ kind: selKind, name: name || kindDef.label, config }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["integrations"] });
      setName("");
      setConfig({});
    },
  });
  const test = useMutation({
    mutationFn: (id: string) => integrationsApi.test(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["integrations"] }),
  });
  const remove = useMutation({
    mutationFn: (id: string) => integrationsApi.remove(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["integrations"] }),
  });

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Integrations</h1>
        <p className="text-xs text-ink-400">Connect LLM providers, observability platforms, and alert channels.</p>
      </div>

      <div className="bg-ink-900 border border-ink-800 rounded p-4 space-y-3 text-sm">
        <div className="grid grid-cols-1 md:grid-cols-3 gap-2">
          <select className="bg-ink-950 border border-ink-700 rounded px-2 py-1"
                  value={selKind} onChange={(e) => { setSelKind(e.target.value); setConfig({}); }}>
            {KINDS.map((k) => (<option key={k.kind} value={k.kind}>{k.label}</option>))}
          </select>
          <input className="bg-ink-950 border border-ink-700 rounded px-2 py-1 md:col-span-2"
                 placeholder="Display name" value={name} onChange={(e) => setName(e.target.value)} />
          {kindDef.fields.map((f) => (
            <input
              key={f}
              className="bg-ink-950 border border-ink-700 rounded px-2 py-1"
              placeholder={f}
              value={config[f] || ""}
              onChange={(e) => setConfig({ ...config, [f]: e.target.value })}
            />
          ))}
        </div>
        <button onClick={() => create.mutate()} disabled={create.isPending}
                className="bg-brand-600 hover:bg-brand-500 disabled:opacity-50 text-white text-sm px-3 py-1.5 rounded">
          Add integration
        </button>
      </div>

      {list.isLoading ? <Loading /> : (
        <div className="bg-ink-900 border border-ink-800 rounded overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-xs uppercase text-ink-400 border-b border-ink-800">
              <tr>
                <th className="text-left px-4 py-2">Kind</th>
                <th className="text-left px-4 py-2">Name</th>
                <th className="text-left px-4 py-2">Status</th>
                <th className="text-left px-4 py-2">Last sync</th>
                <th className="text-left px-4 py-2">Errors</th>
                <th></th>
              </tr>
            </thead>
            <tbody className="divide-y divide-ink-800">
              {(list.data || []).map((i: any) => (
                <tr key={i.id}>
                  <td className="px-4 py-2 text-ink-300">{i.kind}</td>
                  <td className="px-4 py-2">{i.name}</td>
                  <td className="px-4 py-2"><StatusBadge status={i.last_status} /></td>
                  <td className="px-4 py-2 text-ink-400 text-xs">
                    {i.last_sync_at ? new Date(i.last_sync_at).toLocaleString() : "—"}
                  </td>
                  <td className="px-4 py-2 text-ink-300">{i.error_count}</td>
                  <td className="px-4 py-2 text-right space-x-3">
                    <button onClick={() => test.mutate(i.id)} className="text-xs text-brand-300 hover:underline">test</button>
                    <button onClick={() => remove.mutate(i.id)} className="text-xs text-rose-300 hover:underline">delete</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
