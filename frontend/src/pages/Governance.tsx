import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { governanceApi } from "@/api/auth";
import { API_URL } from "@/api/client";
import StatusBadge from "@/components/common/StatusBadge";
import Loading from "@/components/common/Loading";
import MetricCard from "@/components/common/MetricCard";

export default function Governance() {
  const qc = useQueryClient();
  const audit = useQuery({ queryKey: ["audit"], queryFn: () => governanceApi.audit(30) });
  const policies = useQuery({ queryKey: ["policies"], queryFn: governanceApi.policies });
  const lp = useQuery({ queryKey: ["lp"], queryFn: governanceApi.leastPrivilege });
  const sc = useQuery({ queryKey: ["scorecard"], queryFn: governanceApi.scorecard });

  const [form, setForm] = useState({ name: "", description: "", rule: '{"environment":"prod","forbid_pii":true}', action: "block" });
  const create = useMutation({
    mutationFn: () =>
      governanceApi.createPolicy({
        name: form.name,
        description: form.description,
        rule: JSON.parse(form.rule || "{}"),
        action: form.action,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["policies"] });
      setForm({ ...form, name: "" });
    },
  });
  const remove = useMutation({
    mutationFn: (id: string) => governanceApi.deletePolicy(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["policies"] }),
  });

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">Governance</h1>
          <p className="text-xs text-ink-400">Policies, audit log, PII & injection signals, least-privilege.</p>
        </div>
        <div className="flex gap-2">
          <a href={`${API_URL}/api/governance/audit-log/export.csv?days=30`}
             className="text-xs px-3 py-1.5 rounded border border-ink-700 text-ink-200">Audit CSV</a>
          <a href={`${API_URL}/api/governance/audit-log/export.json?days=30`}
             className="text-xs px-3 py-1.5 rounded border border-ink-700 text-ink-200">Audit JSON</a>
        </div>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <MetricCard label="Traces (30d)" value={Number(sc.data?.total_traces_30d || 0).toLocaleString()} />
        <MetricCard label="PII flags (30d)" value={Number(sc.data?.pii_flags_30d || 0).toLocaleString()}
                    tone={(sc.data?.pii_flags_30d || 0) > 0 ? "warn" : "good"} />
        <MetricCard label="Injection attempts" value={Number(sc.data?.injection_attempts_30d || 0).toLocaleString()}
                    tone={(sc.data?.injection_attempts_30d || 0) > 0 ? "bad" : "good"} />
        <MetricCard label="Policy blocks" value={Number(sc.data?.policy_blocks_30d || 0).toLocaleString()} />
      </div>

      <section className="bg-ink-900 border border-ink-800 rounded p-4">
        <h2 className="text-sm uppercase text-ink-400 mb-2">Policies</h2>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-2 text-sm mb-3">
          <input className="bg-ink-950 border border-ink-700 rounded px-2 py-1" placeholder="Name"
                 value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          <input className="bg-ink-950 border border-ink-700 rounded px-2 py-1" placeholder="Description"
                 value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} />
          <textarea className="bg-ink-950 border border-ink-700 rounded px-2 py-1 md:col-span-2 h-20 font-mono text-xs"
                    placeholder='{"agent_id":"...","environment":"prod","forbid_tool":"...","forbid_pii":true,"block_on_injection":true}'
                    value={form.rule} onChange={(e) => setForm({ ...form, rule: e.target.value })} />
          <select className="bg-ink-950 border border-ink-700 rounded px-2 py-1"
                  value={form.action} onChange={(e) => setForm({ ...form, action: e.target.value })}>
            <option value="block">block</option>
            <option value="warn">warn</option>
            <option value="log">log</option>
          </select>
          <button onClick={() => create.mutate()} disabled={!form.name}
                  className="bg-brand-600 hover:bg-brand-500 disabled:opacity-50 text-white px-3 py-1 rounded">
            Create policy
          </button>
        </div>
        <ul className="divide-y divide-ink-800 text-sm">
          {(policies.data || []).map((p: any) => (
            <li key={p.id} className="py-2 flex items-center justify-between">
              <div>
                <div className="font-medium">{p.name} <StatusBadge status={p.action} className="ml-2" /></div>
                <div className="text-xs text-ink-400">{p.description}</div>
                <pre className="text-[10px] text-ink-500 mt-1">{JSON.stringify(p.rule)}</pre>
              </div>
              <button className="text-xs text-rose-300 hover:underline" onClick={() => remove.mutate(p.id)}>delete</button>
            </li>
          ))}
        </ul>
      </section>

      <section className="bg-ink-900 border border-ink-800 rounded p-4">
        <h2 className="text-sm uppercase text-ink-400 mb-2">Least-privilege report</h2>
        {lp.isLoading ? <Loading /> : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-xs uppercase text-ink-400 border-b border-ink-800">
                <tr>
                  <th className="text-left py-2 px-2">Agent</th>
                  <th className="text-left py-2 px-2">Allowed</th>
                  <th className="text-left py-2 px-2">Used (7d)</th>
                  <th className="text-left py-2 px-2">Unused</th>
                  <th className="text-left py-2 px-2">Recommendation</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-ink-800">
                {(lp.data?.agents || []).map((a: any) => (
                  <tr key={a.agent_id}>
                    <td className="py-2 px-2">{a.agent_name}</td>
                    <td className="py-2 px-2 text-ink-300 text-xs">{a.allowed_tools.join(", ") || "—"}</td>
                    <td className="py-2 px-2 text-ink-300 text-xs">{a.used_tools.join(", ") || "—"}</td>
                    <td className="py-2 px-2 text-amber-300 text-xs">{a.unused_permissions.join(", ") || "—"}</td>
                    <td className="py-2 px-2 text-ink-300 text-xs">{a.recommendation}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="bg-ink-900 border border-ink-800 rounded p-4">
        <h2 className="text-sm uppercase text-ink-400 mb-2">Audit log (30d)</h2>
        {audit.isLoading ? <Loading /> : (
          <div className="overflow-x-auto max-h-96 scrollbar">
            <table className="w-full text-sm">
              <thead className="text-xs uppercase text-ink-400 border-b border-ink-800 sticky top-0 bg-ink-900">
                <tr>
                  <th className="text-left py-2 px-2">When</th>
                  <th className="text-left py-2 px-2">Actor</th>
                  <th className="text-left py-2 px-2">Action</th>
                  <th className="text-left py-2 px-2">Resource</th>
                  <th className="text-left py-2 px-2">Rationale</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-ink-800">
                {(audit.data || []).map((a: any) => (
                  <tr key={a.id}>
                    <td className="py-2 px-2 text-ink-400">{new Date(a.created_at).toLocaleString()}</td>
                    <td className="py-2 px-2 text-ink-300">{a.actor_type}/{a.actor_id?.slice(0, 8) || "—"}</td>
                    <td className="py-2 px-2 text-ink-100">{a.action}</td>
                    <td className="py-2 px-2 text-ink-300">{a.resource_type}/{a.resource_id?.slice(0, 8) || "—"}</td>
                    <td className="py-2 px-2 text-ink-400">{a.rationale}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
