import clsx from "clsx";
import StatusBadge from "@/components/common/StatusBadge";
import { Violation } from "@/types/policy";

export const violationKey = (v: Violation) => v.id ?? v.policy_id;

export const counterexamplePending = (v: Violation) => v.counterexample_status === "pending";

type Props = {
  violations: Violation[];
  selected: Violation | null;
  onSelect: (v: Violation) => void;
};

export default function ViolationsPanel({ violations, selected, onSelect }: Props) {
  if (!violations.length) {
    return <div className="text-ink-400 text-sm">No policy violations.</div>;
  }
  return (
    <ul className="space-y-1">
      {violations.map((v) => {
        const active = selected && violationKey(selected) === violationKey(v);
        return (
          <li key={violationKey(v)}>
            <button
              aria-pressed={!!active}
              onClick={() => onSelect(v)}
              className={clsx(
                "w-full text-left border rounded px-3 py-2 text-sm",
                active ? "border-rose-600/60 bg-rose-500/5" : "border-ink-800 hover:bg-ink-900"
              )}
            >
              <div className="flex items-center gap-2">
                <span className="font-medium">{v.policy_name}</span>
                <StatusBadge status={v.severity} />
                <span className="text-xs text-ink-400">{v.mode}</span>
                <span className="text-xs text-ink-400 ml-auto">at event #{v.at_seq}</span>
              </div>
              <div className="text-xs text-ink-300 mt-1">{v.message}</div>
              <div className="text-xs mt-1">
                {counterexamplePending(v) ? (
                  <span className="text-ink-400">Counterexample pending: computed by the batch check once the trace completes.</span>
                ) : v.counterexample_status === "none" ? (
                  <span className="text-ink-400">No counterexample for this violation.</span>
                ) : v.counterexample.length ? (
                  <span className="text-amber-300">
                    Counterexample: {v.counterexample.map((s) => `#${s}`).join(", ")}
                    {v.counterexample_status === "fallback" && (
                      <span className="text-ink-400"> (may not be minimal: the solver did not finish)</span>
                    )}
                  </span>
                ) : (
                  <span className="text-ink-400">Violated regardless of any event in the trace.</span>
                )}
              </div>
            </button>
          </li>
        );
      })}
    </ul>
  );
}
