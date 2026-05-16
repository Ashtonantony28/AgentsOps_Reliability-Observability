import {
  ResponsiveContainer, LineChart, Line, XAxis, YAxis, Tooltip, CartesianGrid, Legend,
} from "recharts";
import { format } from "date-fns";

type Series = {
  name: string;
  color: string;
  points: { t: string | Date; value: number }[];
};

export default function AgentComparisonChart({
  series,
  label,
  formatY,
}: {
  series: Series[];
  label: string;
  formatY?: (v: number) => string;
}) {
  // Build a unified timeline by t timestamp
  const keys: Record<number, any> = {};
  for (const s of series) {
    for (const p of s.points) {
      const t = new Date(p.t).getTime();
      keys[t] = keys[t] || { t };
      keys[t][s.name] = p.value;
    }
  }
  const data = Object.values(keys).sort((a: any, b: any) => a.t - b.t);
  return (
    <div className="bg-ink-900 border border-ink-800 rounded-lg p-4">
      <div className="text-sm text-ink-300 mb-2">{label}</div>
      <ResponsiveContainer width="100%" height={260}>
        <LineChart data={data as any}>
          <CartesianGrid stroke="#222a3b" vertical={false} />
          <XAxis dataKey="t" tickFormatter={(t) => format(new Date(t), "HH:mm")} stroke="#646f81" fontSize={10} />
          <YAxis stroke="#646f81" fontSize={10} tickFormatter={formatY} width={60} />
          <Tooltip
            contentStyle={{ background: "#141a28", border: "1px solid #2a3247", fontSize: 12 }}
            labelFormatter={(t) => format(new Date(t as number), "yyyy-MM-dd HH:mm")}
          />
          <Legend wrapperStyle={{ fontSize: 11 }} />
          {series.map((s) => (
            <Line key={s.name} type="monotone" dataKey={s.name} stroke={s.color} dot={false} strokeWidth={2} />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
