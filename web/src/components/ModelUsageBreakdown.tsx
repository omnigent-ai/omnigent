import { useState } from "react";
import type { ModelUsage } from "@/lib/types";
import { formatSessionCostUsd, formatTokenCount } from "@/lib/formatCost";

/**
 * Token buckets shown per model in the ``usage_by_model`` section, mapping
 * the ``ModelUsage`` field to its row label. Cost is rendered separately.
 */
const MODEL_TOKEN_ROWS: readonly { key: keyof ModelUsage; label: string }[] = [
  { key: "inputTokens", label: "Input" },
  { key: "outputTokens", label: "Output" },
  { key: "cacheReadInputTokens", label: "Cache read" },
  { key: "cacheCreationInputTokens", label: "Cache write" },
  { key: "totalTokens", label: "Total" },
];

/**
 * Per-model usage breakdown: one labeled group per model, each listing its
 * recorded token buckets (and USD cost when the model was priced). Rendered
 * beneath the aggregate token breakdown. The caller decides whether to show
 * this at all (it's redundant with the aggregate when only one model ran).
 *
 * @param usageByModel - Map of raw harness model id to its cumulative usage.
 */
export function ModelUsageBreakdown({
  usageByModel,
  label: sectionLabel = "Token usage",
}: {
  usageByModel: Record<string, ModelUsage>;
  label?: string;
}) {
  const [isOpen, setIsOpen] = useState(false);
  // Stable display order: most total tokens first, so the dominant model
  // leads. Falls back to 0 for models that haven't recorded a total yet.
  const models = Object.entries(usageByModel).sort(
    ([, a], [, b]) => (b.totalTokens ?? 0) - (a.totalTokens ?? 0),
  );
  return (
    <details
      data-testid="agent-info-usage-by-model"
      onToggle={(e) => setIsOpen(e.currentTarget.open)}
    >
      <summary className="cursor-pointer select-none list-none">
        <span className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground/70">
          <span className="inline-flex items-center gap-1">
            {sectionLabel}
            <span className="text-[9px]">{isOpen ? "▼" : "▶"}</span>
          </span>
        </span>
      </summary>
      <div className="mt-1.5 flex flex-col gap-2">
        {models.map(([model, usage]) => {
          const rows = MODEL_TOKEN_ROWS.flatMap(({ key, label }) => {
            const value = usage[key];
            return value != null ? [{ label, value }] : [];
          });
          return (
            <div
              key={model}
              className="flex flex-col gap-0.5"
              data-testid={`agent-info-model-${model}`}
            >
              <span className="truncate font-mono text-sm text-muted-foreground" title={model}>
                {model}
              </span>
              {rows.map((row) => (
                <div
                  key={row.label}
                  className="flex items-baseline justify-between gap-3 pl-2 text-sm"
                >
                  <span className="text-muted-foreground/70">{row.label}</span>
                  <span className="tabular-nums text-muted-foreground">
                    {formatTokenCount(row.value)}
                  </span>
                </div>
              ))}
              {usage.totalCostUsd != null && (
                <div className="flex items-baseline justify-between gap-3 pl-2 text-sm">
                  <span className="text-muted-foreground/70">Cost</span>
                  <span className="tabular-nums text-muted-foreground">
                    {formatSessionCostUsd(usage.totalCostUsd)}
                  </span>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </details>
  );
}
