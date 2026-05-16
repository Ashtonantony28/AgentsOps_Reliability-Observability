import { Loader2 } from "lucide-react";

export default function Loading({ label = "Loading..." }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 text-ink-300 text-sm py-6">
      <Loader2 className="w-4 h-4 animate-spin" /> {label}
    </div>
  );
}
