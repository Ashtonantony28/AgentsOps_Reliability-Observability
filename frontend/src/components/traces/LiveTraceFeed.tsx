import { useWsStore } from "@/stores/wsStore";
import StatusBadge from "@/components/common/StatusBadge";
import { format } from "date-fns";

export default function LiveTraceFeed() {
  const { recent, connected } = useWsStore();
  return (
    <div className="bg-ink-900 border border-ink-800 rounded-lg p-4">
      <div className="flex items-center justify-between mb-3">
        <div className="text-sm text-ink-300">Live trace feed</div>
        <div className={connected ? "text-xs text-emerald-300" : "text-xs text-rose-300"}>
          {connected ? "● live" : "● offline"}
        </div>
      </div>
      <ul className="space-y-1 max-h-80 overflow-y-auto scrollbar">
        {recent.length === 0 && (
          <li className="text-ink-400 text-xs">Waiting for traces…</li>
        )}
        {recent.map((t) => (
          <li key={t.id} className="flex items-center gap-2 text-xs">
            <span className="text-ink-500 w-14 shrink-0">
              {format(new Date(t.created_at), "HH:mm:ss")}
            </span>
            <StatusBadge status={t.status} />
            <span className="text-ink-300 truncate flex-1">{t.input_preview}</span>
            <span className="text-ink-400">{t.latency_ms}ms</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
