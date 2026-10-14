import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { errorMessage } from "@/api/client";
import { agentsApi } from "@/api/agents";
import { policyErrors, tracePoliciesApi } from "@/api/tracePolicies";
import { PolicyAction, PolicyError, TracePolicy } from "@/types/policy";

const TEMPLATE = `policy confirm_before_delete severity high:
  always tool("delete_*") -> once event("confirmation")
`;

export function PolicyErrorList({ source, errors }: { source: string; errors: PolicyError[] }) {
  const lines = source.split("\n");
  return (
    <ul className="space-y-2" role="alert">
      {errors.map((e, i) => (
        <li key={i} className="text-xs text-rose-300">
          <div>Line {e.line}, col {e.col}: {e.message}</div>
          {lines[e.line - 1] !== undefined && (
            <pre className="mt-1 font-mono text-[11px] text-ink-300 bg-ink-950 rounded px-2 py-1 overflow-x-auto">
              {lines[e.line - 1]}
              {"\n"}
              <span className="text-rose-400">{" ".repeat(Math.max(e.col - 1, 0))}^</span>
            </pre>
          )}
        </li>
      ))}
    </ul>
  );
}

type Props = { policy?: TracePolicy; onDone: () => void };

export default function PolicyEditor({ policy, onDone }: Props) {
  const [source, setSource] = useState(policy?.source ?? TEMPLATE);
  const [action, setAction] = useState<PolicyAction>(policy?.action ?? "log");
  const [agentId, setAgentId] = useState(policy?.agent_id ?? "");
  const [environment, setEnvironment] = useState(policy?.environment ?? "");
  const [enabled, setEnabled] = useState(policy?.enabled ?? true);
  const [errors, setErrors] = useState<PolicyError[]>([]);
  const [formError, setFormError] = useState<string | null>(null);
  const agents = useQuery({ queryKey: ["agents"], queryFn: agentsApi.list });

  const latest = useRef(0);
  useEffect(() => {
    const n = ++latest.current;
    const t = setTimeout(() => {
      tracePoliciesApi
        .validate(source)
        .then((r) => n === latest.current && setErrors(r.errors))
        .catch(() => {});
    }, 400);
    return () => clearTimeout(t);
  }, [source]);

  const save = useMutation({
    mutationFn: () => {
      const body = { source, action, agent_id: agentId || null, environment: environment || null, enabled };
      return policy ? tracePoliciesApi.update(policy.id, body) : tracePoliciesApi.create(body);
    },
    onSuccess: onDone,
    onError: (err) => {
      const errs = policyErrors(err);
      latest.current++;
      setErrors(errs);
      setFormError(errs.length ? null : errorMessage(err));
    },
  });

  return (
    <div className="space-y-2 text-sm">
      <textarea
        aria-label="Policy source"
        spellCheck={false}
        className="w-full h-32 bg-ink-950 border border-ink-700 rounded px-2 py-1 font-mono text-xs"
        value={source}
        onChange={(e) => {
          setSource(e.target.value);
          setFormError(null);
        }}
      />
      {errors.length > 0 && <PolicyErrorList source={source} errors={errors} />}
      <div className="flex flex-wrap items-center gap-2">
        <select aria-label="Action" className="bg-ink-950 border border-ink-700 rounded px-2 py-1"
                value={action} onChange={(e) => setAction(e.target.value as PolicyAction)}>
          <option value="log">log</option>
          <option value="alert">alert</option>
          <option value="block">block</option>
        </select>
        <select aria-label="Agent" className="bg-ink-950 border border-ink-700 rounded px-2 py-1"
                value={agentId} onChange={(e) => setAgentId(e.target.value)}>
          <option value="">All agents</option>
          {(agents.data || []).map((a) => (
            <option key={a.id} value={a.id}>{a.name}</option>
          ))}
        </select>
        <input aria-label="Environment" placeholder="Any environment"
               className="bg-ink-950 border border-ink-700 rounded px-2 py-1 w-36"
               value={environment} onChange={(e) => setEnvironment(e.target.value)} />
        <label className="flex items-center gap-1 text-xs text-ink-300">
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} /> enabled
        </label>
        <div className="ml-auto flex gap-2">
          <button className="text-xs px-3 py-1 rounded border border-ink-700 text-ink-200" onClick={onDone}>
            Cancel
          </button>
          <button
            onClick={() => save.mutate()}
            disabled={save.isPending || errors.length > 0}
            className="bg-brand-600 hover:bg-brand-500 disabled:opacity-50 text-white px-3 py-1 rounded"
          >
            {policy ? "Save" : "Create"}
          </button>
        </div>
      </div>
      {formError && <div className="text-xs text-rose-300" role="alert">{formError}</div>}
    </div>
  );
}
