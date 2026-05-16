import { useState } from "react";
import { Span } from "@/types/trace";
import { ChevronRight, ChevronDown } from "lucide-react";
import StatusBadge from "@/components/common/StatusBadge";

export default function TraceSpanTree({ spans }: { spans: Span[] }) {
  if (!spans?.length) {
    return <div className="text-ink-400 text-sm">No spans recorded.</div>;
  }
  return (
    <div className="space-y-1">
      {spans.map((s) => (
        <SpanNode key={s.id} span={s} />
      ))}
    </div>
  );
}

function SpanNode({ span }: { span: Span }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="border border-ink-800 rounded">
      <button
        className="w-full flex items-center gap-2 text-left px-3 py-2 hover:bg-ink-900"
        onClick={() => setOpen((x) => !x)}
      >
        {open ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
        <span className="text-xs text-ink-400">{span.kind}</span>
        <span className="font-medium">{span.name}</span>
        <span className="text-xs text-ink-400">{span.latency_ms} ms</span>
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
