import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ReferenceArea,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  chartRows,
  formatChartValue,
  parseSuggestionChart,
  seriesKey,
  type SuggestionChart as Chart,
} from "@/lib/suggestionChart";

// Theme tokens defined for both light and dark mode.
const SERIES_COLORS = ["var(--chart-1)", "var(--chart-3)", "var(--chart-4)", "var(--chart-5)"];
const AXIS_TICK = { fontSize: 11 };

/** Draws a suggestion's chart, or nothing when this build cannot draw it. */
export function SuggestionChart({ chart: raw }: { chart: unknown }) {
  const chart = parseSuggestionChart(raw);
  if (!chart) return null;
  return (
    <figure data-testid="inbox-suggestion-chart" className="flex flex-col gap-1">
      <figcaption className="text-sm font-medium">{chart.title}</figcaption>
      <div className="h-48">
        <ResponsiveContainer width="100%" height="100%">
          {chart.type === "bar" ? <BarBody chart={chart} /> : <LineBody chart={chart} />}
        </ResponsiveContainer>
      </div>
      {chart.source && (
        <p className="text-xs text-muted-foreground">
          Source:{" "}
          {chart.source.href ? (
            <a
              href={chart.source.href}
              target="_blank"
              rel="noopener noreferrer"
              className="underline underline-offset-2"
            >
              {chart.source.label}
            </a>
          ) : (
            chart.source.label
          )}
        </p>
      )}
    </figure>
  );
}

function Decorations({ chart }: { chart: Chart }) {
  const format = (value: number) => formatChartValue(value, chart.unit);
  return (
    <>
      <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
      <XAxis
        dataKey="x"
        tick={AXIS_TICK}
        tickLine={false}
        axisLine={false}
        interval="preserveStartEnd"
        className="fill-muted-foreground"
      />
      <YAxis
        tick={AXIS_TICK}
        tickLine={false}
        axisLine={false}
        width={56}
        tickFormatter={format}
        className="fill-muted-foreground"
      />
      <Tooltip
        formatter={(value) => (typeof value === "number" ? format(value) : String(value))}
        contentStyle={{
          background: "var(--popover)",
          border: "1px solid var(--border)",
          borderRadius: 6,
          fontSize: 12,
        }}
      />
      {chart.series.length > 1 && <Legend wrapperStyle={{ fontSize: 11 }} />}
      {chart.highlight && (
        <ReferenceArea
          x1={chart.highlight.from}
          x2={chart.highlight.to}
          fill="var(--chart-2)"
          fillOpacity={0.12}
          ifOverflow="extendDomain"
        />
      )}
      {chart.markers.map((marker) => (
        <ReferenceLine
          key={`${marker.x}:${marker.label}`}
          x={marker.x}
          stroke="var(--muted-foreground)"
          strokeDasharray="4 2"
          label={{ value: marker.label, position: "top", fontSize: 10, fill: "currentColor" }}
        />
      ))}
    </>
  );
}

function LineBody({ chart }: { chart: Chart }) {
  return (
    <LineChart data={chartRows(chart)} margin={{ top: 16, right: 8, bottom: 0, left: 0 }}>
      {Decorations({ chart })}
      {chart.series.map((series, index) => (
        <Line
          key={seriesKey(index)}
          dataKey={seriesKey(index)}
          name={series.name}
          type="monotone"
          stroke={SERIES_COLORS[index]}
          strokeWidth={series.emphasis ? 3 : 1.5}
          dot={false}
          connectNulls={false}
          isAnimationActive={false}
        />
      ))}
    </LineChart>
  );
}

function BarBody({ chart }: { chart: Chart }) {
  return (
    <BarChart data={chartRows(chart)} margin={{ top: 16, right: 8, bottom: 0, left: 0 }}>
      {Decorations({ chart })}
      {chart.series.map((series, index) => (
        <Bar
          key={seriesKey(index)}
          dataKey={seriesKey(index)}
          name={series.name}
          fill={SERIES_COLORS[index]}
          fillOpacity={series.emphasis || chart.series.length === 1 ? 1 : 0.6}
          stroke={series.emphasis ? "var(--foreground)" : undefined}
          strokeWidth={series.emphasis ? 1 : 0}
          isAnimationActive={false}
        />
      ))}
    </BarChart>
  );
}
