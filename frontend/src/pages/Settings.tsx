import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { authApi } from "@/api/auth";
import Loading from "@/components/common/Loading";
import StatusBadge from "@/components/common/StatusBadge";
import { errorMessage } from "@/api/client";

export default function Settings() {
  const qc = useQueryClient();
  const me = useQuery({ queryKey: ["me"], queryFn: authApi.me });
  const members = useQuery({ queryKey: ["members"], queryFn: authApi.members });
  const keys = useQuery({ queryKey: ["keys"], queryFn: authApi.listApiKeys });
  const sessions = useQuery({ queryKey: ["sessions"], queryFn: authApi.sessions });

  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteRole, setInviteRole] = useState("developer");
  const [inviteErr, setInviteErr] = useState<string | null>(null);
  const invite = useMutation({
    mutationFn: () => authApi.invite(inviteEmail, inviteRole),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["members"] });
      setInviteEmail("");
    },
    onError: (e: any) => setInviteErr(errorMessage(e)),
  });

  const [keyName, setKeyName] = useState("");
  const [newKey, setNewKey] = useState<string | null>(null);
  const createKey = useMutation({
    mutationFn: () => authApi.createApiKey(keyName),
    onSuccess: (data: any) => {
      setNewKey(data.key);
      setKeyName("");
      qc.invalidateQueries({ queryKey: ["keys"] });
    },
  });
  const revokeKey = useMutation({
    mutationFn: (id: string) => authApi.revokeApiKey(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["keys"] }),
  });
  const revokeSession = useMutation({
    mutationFn: (id: string) => authApi.revokeSession(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["sessions"] }),
  });

  if (me.isLoading) return <Loading />;
  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-xl font-semibold">Settings</h1>
        <p className="text-xs text-ink-400">Organization, members, API keys, sessions.</p>
      </div>

      <section className="bg-ink-900 border border-ink-800 rounded p-4">
        <h2 className="text-sm uppercase text-ink-400 mb-2">Organization</h2>
        {me.data?.organizations?.map((o: any) => (
          <div key={o.id} className="text-sm flex items-center justify-between border-b border-ink-800 py-2 last:border-0">
            <div>
              <div className="font-medium">{o.name}</div>
              <div className="text-xs text-ink-400">{o.slug} · {o.tier} · {o.retention_days}d retention</div>
            </div>
            {me.data.active_org_id === o.id && <StatusBadge status="active" />}
          </div>
        ))}
      </section>

      <section className="bg-ink-900 border border-ink-800 rounded p-4">
        <h2 className="text-sm uppercase text-ink-400 mb-2">Members</h2>
        <ul className="divide-y divide-ink-800 text-sm">
          {(members.data || []).map((m: any) => (
            <li key={m.user_id} className="py-2 flex items-center justify-between">
              <div>
                <div>{m.email}</div>
                <div className="text-xs text-ink-400">{m.full_name}</div>
              </div>
              <StatusBadge status={m.role} />
            </li>
          ))}
        </ul>
        <div className="mt-3 flex gap-2 text-sm">
          <input className="bg-ink-950 border border-ink-700 rounded px-2 py-1 flex-1"
                 placeholder="invite@example.com" value={inviteEmail}
                 onChange={(e) => setInviteEmail(e.target.value)} />
          <select className="bg-ink-950 border border-ink-700 rounded px-2 py-1"
                  value={inviteRole} onChange={(e) => setInviteRole(e.target.value)}>
            <option value="viewer">viewer</option>
            <option value="developer">developer</option>
            <option value="admin">admin</option>
          </select>
          <button onClick={() => invite.mutate()} disabled={!inviteEmail}
                  className="bg-brand-600 hover:bg-brand-500 disabled:opacity-50 text-white px-3 py-1 rounded">
            Invite
          </button>
        </div>
        {inviteErr && <div className="text-xs text-rose-300 mt-2">{inviteErr}</div>}
      </section>

      <section className="bg-ink-900 border border-ink-800 rounded p-4">
        <h2 className="text-sm uppercase text-ink-400 mb-2">API keys</h2>
        <div className="flex gap-2 mb-3 text-sm">
          <input className="bg-ink-950 border border-ink-700 rounded px-2 py-1 flex-1"
                 placeholder="Key name" value={keyName} onChange={(e) => setKeyName(e.target.value)} />
          <button onClick={() => createKey.mutate()} disabled={!keyName}
                  className="bg-brand-600 hover:bg-brand-500 disabled:opacity-50 text-white px-3 py-1 rounded">
            Generate
          </button>
        </div>
        {newKey && (
          <div className="text-xs bg-amber-500/10 border border-amber-700/30 text-amber-200 rounded p-2 mb-3">
            Copy this key now — it won't be shown again: <code className="font-mono">{newKey}</code>
          </div>
        )}
        <ul className="divide-y divide-ink-800 text-sm">
          {(keys.data || []).map((k: any) => (
            <li key={k.id} className="py-2 flex items-center justify-between">
              <div>
                <div className="font-mono">{k.key_prefix}…</div>
                <div className="text-xs text-ink-400">{k.name} · created {new Date(k.created_at).toLocaleString()}</div>
                {k.revoked_at && <StatusBadge status="revoked" />}
              </div>
              {!k.revoked_at && (
                <button onClick={() => revokeKey.mutate(k.id)} className="text-xs text-rose-300 hover:underline">revoke</button>
              )}
            </li>
          ))}
        </ul>
      </section>

      <section className="bg-ink-900 border border-ink-800 rounded p-4">
        <h2 className="text-sm uppercase text-ink-400 mb-2">Active sessions</h2>
        <ul className="divide-y divide-ink-800 text-sm">
          {(sessions.data || []).map((s: any) => (
            <li key={s.id} className="py-2 flex items-center justify-between">
              <div>
                <div className="text-ink-200">{s.user_agent || "unknown"}</div>
                <div className="text-xs text-ink-400">IP {s.ip_address || "—"} · created {new Date(s.created_at).toLocaleString()}</div>
              </div>
              <button onClick={() => revokeSession.mutate(s.id)} className="text-xs text-rose-300 hover:underline">revoke</button>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
