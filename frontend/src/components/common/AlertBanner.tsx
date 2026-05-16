import clsx from "clsx";
import { AlertTriangle, CheckCircle, Info, XCircle } from "lucide-react";

type Props = {
  tone?: "info" | "warn" | "error" | "success";
  title: string;
  children?: React.ReactNode;
};

const ICONS = {
  info: Info,
  warn: AlertTriangle,
  error: XCircle,
  success: CheckCircle,
};

export default function AlertBanner({ tone = "info", title, children }: Props) {
  const Icon = ICONS[tone];
  return (
    <div
      className={clsx(
        "flex items-start gap-3 rounded border p-3",
        tone === "info" && "bg-sky-500/10 text-sky-200 border-sky-700/30",
        tone === "warn" && "bg-amber-500/10 text-amber-200 border-amber-700/30",
        tone === "error" && "bg-rose-500/10 text-rose-200 border-rose-700/30",
        tone === "success" && "bg-emerald-500/10 text-emerald-200 border-emerald-700/30"
      )}
    >
      <Icon className="w-4 h-4 mt-0.5" />
      <div className="text-sm">
        <div className="font-medium">{title}</div>
        {children && <div className="text-xs opacity-80 mt-1">{children}</div>}
      </div>
    </div>
  );
}
