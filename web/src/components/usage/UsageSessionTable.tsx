import { useMemo, useState } from "react";
import { Link } from "@/lib/routing";
import { ArrowDownIcon, ArrowUpIcon } from "lucide-react";
import type { SessionUsage } from "@/lib/usageApi";
import { formatSessionCostUsd } from "@/lib/formatCost";

interface Props {
  sessions: SessionUsage[];
}

type SortKey = "title" | "harness" | "costUsd" | "updatedAt";
type SortDir = "asc" | "desc";

const COLUMNS: { key: SortKey; label: string; align?: "right" }[] = [
  { key: "title", label: "Session" },
  { key: "harness", label: "Harness" },
  { key: "costUsd", label: "Cost", align: "right" },
  { key: "updatedAt", label: "Last active", align: "right" },
];

function primaryModel(models: Record<string, number>): string | null {
  const entries = Object.entries(models);
  if (entries.length === 0) return null;
  entries.sort(([, a], [, b]) => b - a);
  return entries[0][0];
}

function formatRelativeDate(epochSec: number): string {
  const now = Date.now() / 1000;
  const diff = now - epochSec;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  if (diff < 604800) return `${Math.floor(diff / 86400)}d ago`;
  return new Date(epochSec * 1000).toLocaleDateString();
}

function compareSessions(a: SessionUsage, b: SessionUsage, key: SortKey, dir: SortDir): number {
  let cmp = 0;
  switch (key) {
    case "title":
      cmp = (a.title ?? "").localeCompare(b.title ?? "");
      break;
    case "harness":
      cmp = (a.harness ?? "").localeCompare(b.harness ?? "");
      break;
    case "costUsd":
      cmp = a.costUsd - b.costUsd;
      break;
    case "updatedAt":
      cmp = a.updatedAt - b.updatedAt;
      break;
  }
  return dir === "asc" ? cmp : -cmp;
}

export function UsageSessionTable({ sessions }: Props) {
  const [sortKey, setSortKey] = useState<SortKey>("updatedAt");
  const [sortDir, setSortDir] = useState<SortDir>("desc");

  const sorted = useMemo(
    () => [...sessions].sort((a, b) => compareSessions(a, b, sortKey, sortDir)),
    [sessions, sortKey, sortDir],
  );

  function handleSort(key: SortKey) {
    if (key === sortKey) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir("desc");
    }
  }

  if (sessions.length === 0) {
    return (
      <div className="flex h-32 items-center justify-center text-sm text-muted-foreground">
        No sessions
      </div>
    );
  }

  return (
    <div className="overflow-x-auto rounded-lg border border-border">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-border bg-muted/50">
            {COLUMNS.map(({ key, label, align }) => (
              <th
                key={key}
                className={`cursor-pointer select-none px-3 py-2 font-medium text-muted-foreground ${
                  align === "right" ? "text-right" : "text-left"
                }`}
                onClick={() => handleSort(key)}
              >
                <span className="inline-flex items-center gap-1">
                  {label}
                  {sortKey === key &&
                    (sortDir === "asc" ? (
                      <ArrowUpIcon className="h-3 w-3" />
                    ) : (
                      <ArrowDownIcon className="h-3 w-3" />
                    ))}
                </span>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sorted.map((s) => (
            <tr key={s.id} className="border-b border-border last:border-b-0 hover:bg-muted/30">
              <td className="max-w-xs truncate px-3 py-2">
                <Link to={`/c/${s.id}`} className="text-foreground hover:underline">
                  {s.title ?? "Untitled"}
                </Link>
                {primaryModel(s.models) && (
                  <span className="ml-2 text-xs text-muted-foreground">
                    {primaryModel(s.models)}
                    {Object.keys(s.models).length > 1 && (
                      <span
                        className="ml-1.5 inline-flex items-center rounded-full bg-muted px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground"
                        title={Object.entries(s.models)
                          .sort(([, a], [, b]) => b - a)
                          .slice(1)
                          .map(([name]) => name)
                          .join(", ")}
                      >
                        +{Object.keys(s.models).length - 1}
                      </span>
                    )}
                  </span>
                )}
              </td>
              <td className="px-3 py-2 text-muted-foreground">
                {s.harness ?? "—"}
                {s.otherHarnesses && s.otherHarnesses.length > 0 && (
                  <span
                    className="ml-1.5 inline-flex items-center rounded-full bg-muted px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground"
                    title={s.otherHarnesses.join(", ")}
                  >
                    +{s.otherHarnesses.length}
                  </span>
                )}
              </td>
              <td className="px-3 py-2 text-right tabular-nums">
                {formatSessionCostUsd(s.costUsd)}
              </td>
              <td className="whitespace-nowrap px-3 py-2 text-right text-muted-foreground">
                {formatRelativeDate(s.updatedAt)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
