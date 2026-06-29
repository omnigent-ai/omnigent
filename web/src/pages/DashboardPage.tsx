/**
 * Usage dashboard (``/dashboard``).
 *
 * Shows aggregated token + cost usage across the caller's accessible
 * conversations: grand totals plus a per-model breakdown, from /v1/usage.
 * Reached from the left-pane Usage button.
 */

import { RefreshCwIcon } from "lucide-react";
import { PageScroll } from "@/components/PageScroll";
import { Button } from "@/components/ui/button";
import type { UsageCounters } from "@/lib/usageApi";
import { useUsage } from "@/hooks/useUsage";

function fmt(n: number): string {
  return n.toLocaleString();
}

function fmtCost(n: number | undefined): string {
  return n == null ? "—" : `$${n.toFixed(n < 1 ? 4 : 2)}`;
}

function StatCard({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border px-4 py-3">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="mt-1 text-xl font-semibold tabular-nums">{value}</div>
    </div>
  );
}

export function DashboardPage() {
  const { data, isLoading, isError, refetch, isFetching } = useUsage();
  const totals: UsageCounters | undefined = data?.totals;
  const byModel = Object.entries(data?.by_model ?? {}).sort(
    (a, b) => b[1].total_tokens - a[1].total_tokens,
  );

  return (
    <PageScroll>
      <div className="mx-auto w-full max-w-3xl px-4 py-6">
        <div className="mb-4 flex items-center justify-between">
          <h1 className="text-lg font-semibold">Usage</h1>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            aria-label="Refresh usage"
            onClick={() => void refetch()}
            disabled={isFetching}
          >
            <RefreshCwIcon className="size-4" />
          </Button>
        </div>

        {isLoading && <p className="text-sm text-muted-foreground">Loading usage…</p>}
        {isError && <p className="text-sm text-destructive">Failed to load usage.</p>}

        {totals && (
          <>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              <StatCard label="Input tokens" value={fmt(totals.input_tokens)} />
              <StatCard label="Output tokens" value={fmt(totals.output_tokens)} />
              <StatCard label="Total tokens" value={fmt(totals.total_tokens)} />
              <StatCard label="Cost" value={fmtCost(totals.total_cost_usd)} />
            </div>
            <p className="mt-2 text-xs text-muted-foreground">
              Across {fmt(data?.conversations ?? 0)} conversation(s).
            </p>

            <h2 className="mt-6 mb-2 text-sm font-semibold">By model</h2>
            {byModel.length === 0 ? (
              <p className="text-sm text-muted-foreground">No model usage recorded yet.</p>
            ) : (
              <table className="w-full text-sm" data-testid="by-model-table">
                <thead>
                  <tr className="border-b text-left text-xs text-muted-foreground">
                    <th className="py-1 pr-2 font-medium">Model</th>
                    <th className="py-1 px-2 text-right font-medium">Input</th>
                    <th className="py-1 px-2 text-right font-medium">Output</th>
                    <th className="py-1 px-2 text-right font-medium">Total</th>
                    <th className="py-1 pl-2 text-right font-medium">Cost</th>
                  </tr>
                </thead>
                <tbody>
                  {byModel.map(([model, u]) => (
                    <tr key={model} className="border-b last:border-0">
                      <td className="py-1 pr-2 font-mono text-xs">{model}</td>
                      <td className="py-1 px-2 text-right tabular-nums">{fmt(u.input_tokens)}</td>
                      <td className="py-1 px-2 text-right tabular-nums">{fmt(u.output_tokens)}</td>
                      <td className="py-1 px-2 text-right tabular-nums">{fmt(u.total_tokens)}</td>
                      <td className="py-1 pl-2 text-right tabular-nums">
                        {fmtCost(u.total_cost_usd)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </>
        )}
      </div>
    </PageScroll>
  );
}

export default DashboardPage;
