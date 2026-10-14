import { useState } from "react";
import clsx from "clsx";
import { Span } from "@/types/trace";
import { ChevronRight, ChevronDown } from "lucide-react";
import StatusBadge from "@/components/common/StatusBadge";

type Props = {
  spans: Span[];
  counterexample?: number[];
  atSeq?: number | null;
};

export default function TraceSpanTree({ spans, counterexample = [], atSeq = null }: Props) {
  if (!spans?.length) {
    return <div className="text-ink-400 text-sm">No spans recorded.</div>;
  }
  const cx = new Set(counterexample);
  return (
    <div className="space-y-1">
      {spans.map((s, i) => {
        const seq = s.seq ?? i;
        return <SpanNode key={s.id} span={s} seq={seq} inCounterexample={cx.has(seq)} violatedHere={seq === atSeq} />;
      })}
    </div>
  );
}

type NodeProps = { span: Span; seq: number; inCounterexample: boolean; violatedHere: boolean };

function SpanNode({ span, seq, inCounterexample, violatedHere }: NodeProps) {
  const [open, setOpen] = useState(false);
  return (
    <div
      data-seq={seq}
      data-counterexample={inCounterexample || undefined}
      className={clsx(
        "border rounded",
        violatedHere ? "border-rose-500/70" : inCounterexample ? "border-amber-500/60" : "border-ink-800",
        inCounterexample && "bg-amber-500/5"
      )}
    >
      <button
        className="w-full flex items-center gap-2 text-left px-3 py-2 hover:bg-ink-900"
        onClick={() => setOpen((x) => !x)}
      >
        {open ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
        <span className="text-xs text-ink-500 tabular-nums">#{seq}</span>
        <span className="text-xs text-ink-400">{span.kind}</span>
        <span className="font-medium">{span.name}</span>
        <span className="text-xs text-ink-400">{span.latency_ms} ms</span>
        {inCounterexample && <span className="text-[10px] uppercase text-amber-300">counterexample</span>}
        {violatedHere && <span className="text-[10px] uppercase text-rose-300">violated here</span>}
        <StatusBadge status={span.status} className="ml-auto" />
      </button>
      {open && (
        <div className="px-3 pb-2 grid grid-cols-2 gap-2 text-xs">
          <div className="bg-ink-950 p-2 rounded">
            <div className="text-ink-400 mb-1">input</div>
            <pre className="text-ink-200 whitespace-pre-wrap break-words">{JSON.stringify(span.input, null, 2)}</pre>
          </div>
          <div className="bg-ink-950 p-2 rounded">
            <div className="text-ink-400 mb-1">output</div>
            <pre className="text-ink-200 whitespace-pre-wrap break-words">{JSON.stringify(span.output, null, 2)}</pre>
          </div>
        </div>
      )}
    </div>
  );
}
