import clsx from "clsx";

type Props = {
  label: string;
  value: string | number;
  hint?: string;
  trend?: "up" | "down" | "flat";
  tone?: "default" | "good" | "warn" | "bad";
};

export default function MetricCard({ label, value, hint, trend, tone = "default" }: Props) {
  const toneCls = {
    default: "text-ink-100",
    good: "text-emerald-300",
    warn: "text-amber-300",
    bad: "text-rose-300",
  }[tone];

  return (
    <div className="bg-ink-900 border border-ink-800 rounded-lg p-4">
      <div className="text-xs text-ink-400 uppercase tracking-wide">{label}</div>
      <div className={clsx("text-2xl font-semibold mt-1", toneCls)}>{value}</div>
      {hint && <div className="text-xs text-ink-400 mt-1">{hint}</div>}
      {trend && (
        <div className="text-[10px] mt-1 text-ink-500">
          {trend === "up" ? "▲" : trend === "down" ? "▼" : "•"} {trend}
        </div>
      )}
    </div>
  );
}
