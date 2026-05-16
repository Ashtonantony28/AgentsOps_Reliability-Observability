import { ResponsiveContainer, AreaChart, Area, XAxis, YAxis, Tooltip, CartesianGrid } from "recharts";
import { format } from "date-fns";
import { TimeSeriesPoint } from "@/types/trace";

type Props = {
  data: TimeSeriesPoint[];
  label: string;
  color?: string;
  formatY?: (v: number) => string;
  height?: number;
};

export default function MetricTimeSeries({ data, label, color = "#7c5cff", formatY, height = 200 }: Props) {
  const points = data.map((p) => ({
    t: new Date(p.t).getTime(),
    value: p.value,
  }));
  return (
    <div className="bg-ink-900 border border-ink-800 rounded-lg p-4">
      <div className="text-sm text-ink-300 mb-2">{label}</div>
      <ResponsiveContainer width="100%" height={height}>
        <AreaChart data={points} margin={{ top: 6, right: 6, left: 0, bottom: 0 }}>
          <defs>
            <linearGradient id={`g-${label}`} x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%" stopColor={color} stopOpacity={0.5} />
              <stop offset="95%" stopColor={color} stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid stroke="#222a3b" vertical={false} />
          <XAxis
            dataKey="t"
            tickFormatter={(t) => format(new Date(t), "HH:mm")}
            stroke="#646f81"
            fontSize={10}
            minTickGap={20}
          />
          <YAxis
            stroke="#646f81"
            fontSize={10}
            tickFormatter={(v) => (formatY ? formatY(v) : String(v))}
            width={60}
          />
          <Tooltip
            contentStyle={{ background: "#141a28", border: "1px solid #2a3247", fontSize: 12 }}
            labelFormatter={(t) => format(new Date(t as number), "yyyy-MM-dd HH:mm")}
            formatter={(v: any) => [formatY ? formatY(Number(v)) : v, label]}
          />
          <Area
            type="monotone"
            dataKey="value"
            stroke={color}
            fill={`url(#g-${label})`}
            strokeWidth={2}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
