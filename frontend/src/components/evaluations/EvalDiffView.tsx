import { EvalDiff } from "@/types/evaluation";

export default function EvalDiffView({ diff }: { diff: EvalDiff }) {
  return (
    <div className="space-y-4">
      <div className="bg-ink-900 border border-ink-800 rounded p-4 grid grid-cols-3 gap-4 text-sm">
        <div>
          <div className="text-ink-400 text-xs">Run A</div>
          <div className="font-medium">{diff.run_a.id.slice(0, 8)}</div>
          <div className="text-ink-300">Score: {diff.run_a.score.toFixed(3)}</div>
        </div>
        <div className="text-center">
          <div className="text-ink-400 text-xs">Δ Score</div>
          <div className={`text-xl font-semibold ${diff.delta_score >= 0 ? "text-emerald-300" : "text-rose-300"}`}>
            {diff.delta_score >= 0 ? "+" : ""}
            {diff.delta_score.toFixed(3)}
          </div>
        </div>
        <div className="text-right">
          <div className="text-ink-400 text-xs">Run B</div>
          <div className="font-medium">{diff.run_b.id.slice(0, 8)}</div>
          <div className="text-ink-300">Score: {diff.run_b.score.toFixed(3)}</div>
        </div>
      </div>
      <div className="grid grid-cols-2 gap-4 text-sm">
        <div className="bg-ink-900 border border-ink-800 rounded p-4">
          <div className="text-rose-300 font-medium mb-2">Regressions ({diff.regressions.length})</div>
          {diff.regressions.length === 0 ? (
            <div className="text-ink-400 text-xs">None</div>
          ) : (
            <ul className="text-xs space-y-1">
              {diff.regressions.map((r) => (
                <li key={r.case_id} className="text-ink-300">
                  {r.case_id.slice(0, 8)}: {r.from.toFixed(2)} → {r.to.toFixed(2)}
                </li>
              ))}
            </ul>
          )}
        </div>
        <div className="bg-ink-900 border border-ink-800 rounded p-4">
          <div className="text-emerald-300 font-medium mb-2">Improvements ({diff.improvements.length})</div>
          {diff.improvements.length === 0 ? (
            <div className="text-ink-400 text-xs">None</div>
          ) : (
            <ul className="text-xs space-y-1">
              {diff.improvements.map((r) => (
                <li key={r.case_id} className="text-ink-300">
                  {r.case_id.slice(0, 8)}: {r.from.toFixed(2)} → {r.to.toFixed(2)}
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
}
