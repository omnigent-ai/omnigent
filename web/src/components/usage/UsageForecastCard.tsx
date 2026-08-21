import { TrendingDownIcon, TrendingUpIcon, MinusIcon } from "lucide-react";
import type { UsageForecast } from "@/lib/usageApi";
import { formatSessionCostUsd } from "@/lib/formatCost";

interface Props {
  forecast: UsageForecast;
}

const TREND_CONFIG = {
  increasing: {
    icon: TrendingUpIcon,
    label: "Increasing",
    color: "text-orange-500",
    bg: "bg-orange-500/10",
  },
  decreasing: {
    icon: TrendingDownIcon,
    label: "Decreasing",
    color: "text-green-500",
    bg: "bg-green-500/10",
  },
  stable: {
    icon: MinusIcon,
    label: "Stable",
    color: "text-muted-foreground",
    bg: "bg-muted",
  },
} as const;

export function UsageForecastCard({ forecast }: Props) {
  const config = TREND_CONFIG[forecast.trend as keyof typeof TREND_CONFIG] ?? TREND_CONFIG.stable;
  const Icon = config.icon;

  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <div className="flex items-center justify-between">
        <div>
          <p className="text-xs text-muted-foreground">Projected next 30 days</p>
          <p className="mt-1 text-2xl font-semibold tabular-nums">
            {formatSessionCostUsd(forecast.projectedCost30d)}
          </p>
        </div>
        <div className={`flex items-center gap-1.5 rounded-full px-2.5 py-1 ${config.bg}`}>
          <Icon className={`h-3.5 w-3.5 ${config.color}`} />
          <span className={`text-xs font-medium ${config.color}`}>{config.label}</span>
        </div>
      </div>
      <p className="mt-2 text-xs text-muted-foreground">
        Based on weighted average of recent daily spend
      </p>
    </div>
  );
}
