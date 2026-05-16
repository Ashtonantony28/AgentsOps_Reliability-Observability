import { ResponsiveContainer, BarChart, Bar, XAxis, YAxis, Tooltip, CartesianGrid } from "recharts";
import { format } from "date-fns";
import { CostBurn } from "@/types/trace";

export default function CostBurnChart({ data }: { data: CostBurn }) {
  const points = data.daily.map((p) => ({
    t: new Date(p.t).getTime(),
    cost: Number(p.value),
  }));
  return (
    <div className="bg-ink-900 border border-ink-800 rounded-lg p-4">
      <div className="flex items-center justify-between mb-2">
        <div className="text-sm text-ink-300">Daily cost (USD)</div>
        <div className="text-xs text-ink-400">
          Projection (30d): <span className="text-ink-100 font-medium">${data.monthly_projection_usd.toFixed(2)}</span>
        </div>
      </div>
      <ResponsiveContainer width="100%" height={220}>
        <BarChart data={points} margin={{ top: 6, right: 6, left: 0, bottom: 0 }}>
          <CartesianGrid stroke="#222a3b" vertical={false} />
          <XAxis
            dataKey="t"
            tickFormatter={(t) => format(new Date(t), "MMM d")}
            stroke="#646f81"
            fontSize={10}
          />
          <YAxis stroke="#646f81" fontSize={10} width={50} />
          <Tooltip
            contentStyle={{ background: "#141a28", border: "1px solid #2a3247", fontSize: 12 }}
            labelFormatter={(t) => format(new Date(t as number), "yyyy-MM-dd")}
            formatter={(v: any) => [`$${Number(v).toFixed(2)}`, "Cost"]}
          />
          <Bar dataKey="cost" fill="#7c5cff" radius={[3, 3, 0, 0]} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
