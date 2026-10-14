import { useEffect } from "react";
import { Link } from "react-router-dom";
import { ShieldAlert, X } from "lucide-react";
import { useWsStore } from "@/stores/wsStore";
import StatusBadge from "@/components/common/StatusBadge";
import { Violation } from "@/types/policy";

export const TOAST_MS = 8000;

export default function ViolationToasts() {
  const toasts = useWsStore((s) => s.toasts);
  return (
    <div className="fixed bottom-4 right-4 z-50 space-y-2 w-80">
      {toasts.map((v) => (
        <Toast key={`${v.id ?? v.policy_id}:${v.detected_at}`} v={v} />
      ))}
    </div>
  );
}

function Toast({ v }: { v: Violation }) {
  const dismiss = useWsStore((s) => s.dismissToast);
  useEffect(() => {
    const t = setTimeout(() => dismiss(v), TOAST_MS);
    return () => clearTimeout(t);
  }, [v, dismiss]);
  return (
    <div role="status" className="bg-ink-900 border border-rose-700/40 rounded p-3 shadow-lg text-sm">
      <div className="flex items-center gap-2">
        <ShieldAlert className="w-4 h-4 text-rose-300 shrink-0" />
        <span className="font-medium truncate">{v.policy_name}</span>
        <StatusBadge status={v.severity} />
        <button aria-label="Dismiss" className="ml-auto text-ink-400 hover:text-ink-100" onClick={() => dismiss(v)}>
          <X className="w-3 h-3" />
        </button>
      </div>
      <div className="text-xs text-ink-300 mt-1">{v.message}</div>
      {v.trace_id && (
        <Link to={`/traces/${v.trace_id}`} className="text-xs text-brand-500 hover:underline" onClick={() => dismiss(v)}>
          Open trace at event #{v.at_seq}
        </Link>
      )}
    </div>
  );
}
