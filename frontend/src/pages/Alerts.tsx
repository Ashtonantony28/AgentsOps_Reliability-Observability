import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { alertsApi } from "@/api/alerts";
import { agentsApi } from "@/api/agents";
import StatusBadge from "@/components/common/StatusBadge";
import Loading from "@/components/common/Loading";
import { errorMessage } from "@/api/client";

export default function Alerts() {
  const qc = useQueryClient();
  const rules = useQuery({ queryKey: ["alert-rules"], queryFn: alertsApi.listRules });
  const events = useQuery({ queryKey: ["alert-events"], queryFn: () => alertsApi.listEvents() });
  const agents = useQuery({ queryKey: ["agents"], queryFn: agentsApi.list });

  const [open, setOpen] = useState(false);
  const [form, setForm] = useState<any>({
    name: "",
    agent_id: "",
    metric: "error_rate",
    operator: ">",
    threshold: 0.05,
    window_minutes: 15,
    severity: "warning",
    channels: "slack:https://hooks.slack.example/foo",
    runbook_url: "",
  });
  const [err, setErr] = useState<string | null>(null);

  const create = useMutation({
    mutationFn: () => {
      const channels = form.channels.split(",").map((s: string) => s.trim()).filter(Boolean).map((tok: string) => {
        const [type, ...rest] = tok.split(":");
        return { type, target: rest.join(":") };
      });
      return alertsApi.createRule({
        name: form.name,
        agent_id: form.agent_id || null,
        metric: form.metric,
        operator: form.operator,
        threshold: Number(form.threshold),
        window_minutes: Number(form.window_minutes),
        severity: form.severity,
        channels,
        runbook_url: form.runbook_url,
      });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["alert-rules"] });
      setOpen(false);
    },
    onError: (e: any) => setErr(errorMessage(e)),
  });

  const ack = useMutation({
    mutationFn: (id: string) => alertsApi.ack(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["alert-events"] }),
  });
  const resolve = useMutation({
    mutationFn: (id: string) => alertsApi.resolve(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["alert-events"] }),
  });
  const remove = useMutation({
    mutationFn: (id: string) => alertsApi.deleteRule(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["alert-rules"] }),
  });

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">Alerts</h1>
          <p className="text-xs text-ink-400">Define alert rules and triage firing alerts.</p>
        </div>
        <button onClick={() => setOpen(!open)} className="bg-brand-600 hover:bg-brand-500 text-white text-sm px-3 py-1.5 rounded">
          + New rule
        </button>
      </div>

      {open && (
        <div className="bg-ink-900 border border-ink-800 rounded p-4 grid grid-cols-1 md:grid-cols-3 gap-3 text-sm">
          <input className="bg-ink-950 border border-ink-700 rounded px-2 py-1" placeholder="Name"
                 value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          <select className="bg-ink-950 border border-ink-700 rounded px-2 py-1"
                  value={form.agent_id} onChange={(e) => setForm({ ...form, agent_id: e.target.value })}>
            <option value="">All agents (org-wide)</option>
            {(agents.data || []).map((a) => (<option key={a.id} value={a.id}>{a.name}</option>))}
          </select>
          <select className="bg-ink-950 border border-ink-700 rounded px-2 py-1"
                  value={form.metric} onChange={(e) => setForm({ ...form, metric: e.target.value })}>
            <option value="error_rate">error_rate</option>
            <option value="latency_p95">latency_p95</option>
            <option value="latency_avg">latency_avg</option>
            <option value="cost_per_day">cost_per_day</option>
            <option value="token_usage">token_usage</option>
            <option value="policy_violations">policy_violations</option>
          </select>
          <select className="bg-ink-950 border border-ink-700 rounded px-2 py-1"
                  value={form.operator} onChange={(e) => setForm({ ...form, operator: e.target.value })}>
            <option value=">">{`>`}</option>
            <option value=">=">{`>=`}</option>
            <option value="<">{`<`}</option>
            <option value="<=">{`<=`}</option>
          </select>
          <input className="bg-ink-950 border border-ink-700 rounded px-2 py-1" type="number" step="any"
                 placeholder="threshold" value={form.threshold}
                 onChange={(e) => setForm({ ...form, threshold: e.target.value })} />
          <input className="bg-ink-950 border border-ink-700 rounded px-2 py-1" type="number"
                 placeholder="window min" value={form.window_minutes}
                 onChange={(e) => setForm({ ...form, window_minutes: e.target.value })} />
          <select className="bg-ink-950 border border-ink-700 rounded px-2 py-1"
                  value={form.severity} onChange={(e) => setForm({ ...form, severity: e.target.value })}>
            <option value="info">info</option>
            <option value="warning">warning</option>
            <option value="critical">critical</option>
          </select>
          <input className="bg-ink-950 border border-ink-700 rounded px-2 py-1 md:col-span-2"
                 placeholder="channels (e.g. slack:https://hooks…,email:on@oncall.example)"
                 value={form.channels} onChange={(e) => setForm({ ...form, channels: e.target.value })} />
          <input className="bg-ink-950 border border-ink-700 rounded px-2 py-1 md:col-span-3"
                 placeholder="runbook URL" value={form.runbook_url}
                 onChange={(e) => setForm({ ...form, runbook_url: e.target.value })} />
          <button onClick={() => create.mutate()} disabled={!form.name || create.isPending}
                  className="md:col-span-3 bg-brand-600 hover:bg-brand-500 disabled:opacity-50 text-white py-1.5 rounded">
            Create rule
          </button>
          {err && <div className="text-xs text-rose-300 md:col-span-3">{err}</div>}
        </div>
      )}

      <section>
        <h2 className="text-sm uppercase text-ink-400 mb-2">Rules</h2>
        {rules.isLoading ? <Loading /> : (
          <div className="bg-ink-900 border border-ink-800 rounded overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-xs uppercase text-ink-400 border-b border-ink-800">
                <tr>
                  <th className="text-left px-4 py-2">Name</th>
                  <th className="text-left px-4 py-2">Condition</th>
                  <th className="text-left px-4 py-2">Window</th>
                  <th className="text-left px-4 py-2">Severity</th>
                  <th className="text-left px-4 py-2">Channels</th>
                  <th></th>
                </tr>
              </thead>
              <tbody className="divide-y divide-ink-800">
                {(rules.data || []).map((r) => (
                  <tr key={r.id}>
                    <td className="px-4 py-2">{r.name}</td>
                    <td className="px-4 py-2 text-ink-300">{r.metric} {r.operator} {r.threshold}</td>
                    <td className="px-4 py-2 text-ink-300">{r.window_minutes}m</td>
                    <td className="px-4 py-2"><StatusBadge status={r.severity} /></td>
                    <td className="px-4 py-2 text-xs text-ink-400">{(r.channels || []).map((c: any) => c.type).join(", ")}</td>
                    <td className="px-4 py-2 text-right">
                      <button onClick={() => remove.mutate(r.id)} className="text-xs text-rose-300 hover:underline">delete</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section>
        <h2 className="text-sm uppercase text-ink-400 mb-2">Recent events</h2>
        <div className="bg-ink-900 border border-ink-800 rounded overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-xs uppercase text-ink-400 border-b border-ink-800">
              <tr>
                <th className="text-left px-4 py-2">Fired</th>
                <th className="text-left px-4 py-2">Status</th>
                <th className="text-left px-4 py-2">Message</th>
                <th className="text-left px-4 py-2">Value</th>
                <th></th>
              </tr>
            </thead>
            <tbody className="divide-y divide-ink-800">
              {(events.data || []).map((e) => (
                <tr key={e.id}>
                  <td className="px-4 py-2 text-ink-300">{new Date(e.fired_at).toLocaleString()}</td>
                  <td className="px-4 py-2"><StatusBadge status={e.status} /></td>
                  <td className="px-4 py-2 text-ink-200">{e.message}</td>
                  <td className="px-4 py-2 text-ink-300">{e.value}</td>
                  <td className="px-4 py-2 text-right space-x-2">
                    {e.status === "firing" && (
                      <button onClick={() => ack.mutate(e.id)} className="text-xs text-amber-300 hover:underline">ack</button>
                    )}
                    {e.status !== "resolved" && (
                      <button onClick={() => resolve.mutate(e.id)} className="text-xs text-emerald-300 hover:underline">resolve</button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
