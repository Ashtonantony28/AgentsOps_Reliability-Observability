import clsx from "clsx";

type Props = {
  status: string;
  className?: string;
};

const TONES: Record<string, string> = {
  healthy: "bg-emerald-500/15 text-emerald-300 border-emerald-700/30",
  success: "bg-emerald-500/15 text-emerald-300 border-emerald-700/30",
  ok: "bg-emerald-500/15 text-emerald-300 border-emerald-700/30",
  resolved: "bg-emerald-500/15 text-emerald-300 border-emerald-700/30",
  degraded: "bg-amber-500/15 text-amber-300 border-amber-700/30",
  warning: "bg-amber-500/15 text-amber-300 border-amber-700/30",
  acknowledged: "bg-amber-500/15 text-amber-300 border-amber-700/30",
  staging: "bg-amber-500/15 text-amber-300 border-amber-700/30",
  down: "bg-rose-500/15 text-rose-300 border-rose-700/30",
  error: "bg-rose-500/15 text-rose-300 border-rose-700/30",
  failed: "bg-rose-500/15 text-rose-300 border-rose-700/30",
  critical: "bg-rose-500/15 text-rose-300 border-rose-700/30",
  firing: "bg-rose-500/15 text-rose-300 border-rose-700/30",
  open: "bg-rose-500/15 text-rose-300 border-rose-700/30",
  prod: "bg-violet-500/15 text-violet-300 border-violet-700/30",
  dev: "bg-sky-500/15 text-sky-300 border-sky-700/30",
};

export default function StatusBadge({ status, className }: Props) {
  const tone = TONES[status?.toLowerCase()] || "bg-ink-700 text-ink-200 border-ink-600";
  return (
    <span
      className={clsx(
        "inline-flex items-center px-2 py-0.5 rounded text-xs border font-medium",
        tone,
        className
      )}
    >
      {status}
    </span>
  );
}
