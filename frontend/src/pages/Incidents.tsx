import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { incidentsApi } from "@/api/alerts";
import StatusBadge from "@/components/common/StatusBadge";
import Loading from "@/components/common/Loading";

export default function Incidents() {
  const qc = useQueryClient();
  const [filter, setFilter] = useState<string | "">("");
  const incidents = useQuery({ queryKey: ["incidents", filter], queryFn: () => incidentsApi.list(filter || undefined) });
  const [selected, setSelected] = useState<string | null>(null);
  const detail = useQuery({
    queryKey: ["incident", selected],
    queryFn: () => incidentsApi.get(selected!),
    enabled: !!selected,
  });
  const resolve = useMutation({
    mutationFn: (id: string) => incidentsApi.resolve(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["incidents"] });
      qc.invalidateQueries({ queryKey: ["incident", selected] });
    },
  });

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">Incidents</h1>
          <p className="text-xs text-ink-400">Auto-generated incidents with timeline & narrative.</p>
        </div>
        <select value={filter} onChange={(e) => setFilter(e.target.value)}
                className="bg-ink-950 border border-ink-700 rounded px-2 py-1 text-sm">
          <option value="">All</option>
          <option value="open">Open</option>
          <option value="investigating">Investigating</option>
          <option value="resolved">Resolved</option>
        </select>
      </div>
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="bg-ink-900 border border-ink-800 rounded">
          {incidents.isLoading ? <Loading /> : (
            <ul className="divide-y divide-ink-800">
              {(incidents.data || []).map((i) => (
                <li key={i.id}>
                  <button
                    onClick={() => setSelected(i.id)}
                    className={`w-full text-left p-3 hover:bg-ink-900/60 ${selected === i.id ? "bg-brand-500/10" : ""}`}
                  >
                    <div className="flex items-center justify-between">
                      <span className="font-medium text-sm">{i.title}</span>
                      <StatusBadge status={i.status} />
                    </div>
                    <div className="text-xs text-ink-400 mt-1">
                      {new Date(i.started_at).toLocaleString()} · <StatusBadge status={i.severity} />
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
        <div>
          {selected && detail.data ? (
            <div className="bg-ink-900 border border-ink-800 rounded p-4">
              <h2 className="font-semibold text-lg">{detail.data.title}</h2>
              <div className="flex items-center gap-2 mt-1">
                <StatusBadge status={detail.data.status} />
                <StatusBadge status={detail.data.severity} />
                <span className="text-xs text-ink-400">started {new Date(detail.data.started_at).toLocaleString()}</span>
              </div>
              <p className="text-sm mt-3 text-ink-200">{detail.data.narrative || "Building narrative…"}</p>
              <div className="mt-4">
                <div className="text-xs uppercase text-ink-400 mb-1">Timeline</div>
                <ul className="text-xs space-y-1">
                  {(detail.data.timeline || []).map((t, idx) => (
                    <li key={idx}>
                      <span className="text-ink-500">{new Date(t.ts).toLocaleString()}</span>
                      <span className="text-ink-200 ml-2">{t.event}</span>
                      {t.detail && <span className="text-ink-400 ml-2">— {t.detail}</span>}
                    </li>
                  ))}
                </ul>
              </div>
              {detail.data.status !== "resolved" && (
                <button onClick={() => resolve.mutate(detail.data.id)}
                        className="mt-4 bg-emerald-600 hover:bg-emerald-500 text-white text-xs px-3 py-1.5 rounded">
                  Mark resolved
                </button>
              )}
            </div>
          ) : (
            <div className="text-sm text-ink-400 p-4">Select an incident to see details.</div>
          )}
        </div>
      </div>
    </div>
  );
}
