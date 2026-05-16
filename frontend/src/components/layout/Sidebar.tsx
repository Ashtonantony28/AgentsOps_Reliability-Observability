import { NavLink } from "react-router-dom";
import {
  Activity,
  AlertTriangle,
  Bot,
  FileBarChart,
  GitBranch,
  Layers,
  ListChecks,
  Plug,
  Settings,
  Shield,
  Siren,
} from "lucide-react";
import clsx from "clsx";

const NAV = [
  { to: "/", label: "Dashboard", icon: Activity },
  { to: "/agents", label: "Agents", icon: Bot },
  { to: "/traces", label: "Traces", icon: Layers },
  { to: "/evaluations", label: "Evaluations", icon: ListChecks },
  { to: "/alerts", label: "Alerts", icon: AlertTriangle },
  { to: "/incidents", label: "Incidents", icon: Siren },
  { to: "/governance", label: "Governance", icon: Shield },
  { to: "/integrations", label: "Integrations", icon: Plug },
  { to: "/reports", label: "Reports", icon: FileBarChart },
  { to: "/settings", label: "Settings", icon: Settings },
];

export default function Sidebar() {
  return (
    <aside className="w-56 shrink-0 bg-ink-950 border-r border-ink-800 flex flex-col">
      <div className="px-4 py-4 border-b border-ink-800 flex items-center gap-2">
        <GitBranch className="w-5 h-5 text-brand-500" />
        <span className="font-semibold tracking-tight">AgentOps</span>
      </div>
      <nav className="flex-1 p-2 space-y-0.5 text-sm">
        {NAV.map(({ to, label, icon: Icon }) => (
          <NavLink
            key={to}
            to={to}
            end={to === "/"}
            className={({ isActive }) =>
              clsx(
                "flex items-center gap-2 px-3 py-2 rounded-md",
                isActive
                  ? "bg-brand-600/15 text-brand-300"
                  : "text-ink-300 hover:bg-ink-900 hover:text-ink-100"
              )
            }
          >
            <Icon className="w-4 h-4" />
            <span>{label}</span>
          </NavLink>
        ))}
      </nav>
      <div className="p-2 text-[10px] text-ink-500">v1.0.0 · {new Date().getFullYear()}</div>
    </aside>
  );
}
