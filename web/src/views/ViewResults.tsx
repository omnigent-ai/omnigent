import { useState } from "react";
import {
  ChevronDownIcon,
  ChevronRightIcon,
  CircleCheckIcon,
  CircleDashedIcon,
  CircleDotIcon,
  FolderIcon,
  MessageSquareIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { relativeTime } from "@/lib/relativeTime";
import { cn } from "@/lib/utils";
import type { ViewDefinition, ViewRecord } from "@/lib/workViewsApi";
import { groupViewRecords, PRIORITIES, statusColor, statusLabel } from "./viewPresentation";

export function StatusIcon({ status }: { status?: string }) {
  const Icon =
    status === "done"
      ? CircleCheckIcon
      : ["running", "in_progress", "review"].includes(status ?? "")
        ? CircleDotIcon
        : CircleDashedIcon;
  return (
    <Icon aria-label={statusLabel(status)} className={cn("size-4 shrink-0", statusColor(status))} />
  );
}

function RecordProperties({
  record,
  definition,
}: {
  record: ViewRecord;
  definition: ViewDefinition;
}) {
  return (
    <div className="flex shrink-0 flex-wrap items-center gap-2 text-xs text-muted-foreground">
      {definition.display.properties.map((property) => {
        if (property === "status" && record.status)
          return (
            <span
              key={property}
              className={cn("rounded-full bg-muted/60 px-2 py-1", statusColor(record.status))}
            >
              {statusLabel(record.status)}
            </span>
          );
        if (property === "priority" && record.priority !== undefined)
          return (
            <span
              key={property}
              className={cn(
                "rounded-full border border-border/70 px-2 py-1",
                record.priority === 1 && "text-red-600 dark:text-red-400",
              )}
            >
              {PRIORITIES[record.priority]}
            </span>
          );
        if (property === "project" && record.project_name)
          return (
            <span
              key={property}
              className="inline-flex max-w-48 items-center gap-1 truncate rounded-full border border-border/70 px-2 py-1"
            >
              <FolderIcon className="size-3" />
              {record.project_name}
            </span>
          );
        if (property === "created_at" || property === "updated_at")
          return (
            <span key={property} title={new Date(record[property] * 1000).toLocaleString()}>
              {property === "created_at" ? "Created" : "Updated"}{" "}
              {relativeTime(record[property] * 1000)}
            </span>
          );
        return null;
      })}
    </div>
  );
}

export function ViewResults({
  records,
  definition,
  onOpen,
}: {
  records: ViewRecord[];
  definition: ViewDefinition;
  onOpen: (record: ViewRecord) => void;
}) {
  const groups = groupViewRecords(records, definition);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const board = definition.display.layout === "board";
  return (
    <div
      data-testid="view-results"
      aria-label={`${definition.entity_type} ${definition.display.layout}`}
      className={cn(
        "min-h-0",
        board &&
          definition.display.group_by !== "none" &&
          "flex items-start gap-3 overflow-x-auto pb-4",
      )}
    >
      {groups.map((group) => (
        <section
          key={group.key}
          aria-label={group.title}
          className={cn(
            board
              ? definition.display.group_by === "none"
                ? "w-full"
                : "w-80 shrink-0 rounded-xl bg-muted/30 p-2"
              : "mb-5",
          )}
        >
          {definition.display.group_by !== "none" && (
            <button
              type="button"
              className="mb-2 flex w-full items-center gap-2 rounded-lg bg-muted/50 px-3 py-2.5 text-left text-sm font-medium hover:bg-muted"
              onClick={() =>
                setCollapsed((previous) => {
                  const next = new Set(previous);
                  if (next.has(group.key)) next.delete(group.key);
                  else next.add(group.key);
                  return next;
                })
              }
              aria-expanded={!collapsed.has(group.key)}
            >
              {collapsed.has(group.key) ? (
                <ChevronRightIcon className="size-3.5" />
              ) : (
                <ChevronDownIcon className="size-3.5" />
              )}
              {definition.display.group_by === "status" && <StatusIcon status={group.key} />}
              {group.title}
              <span className="ml-1 font-normal text-muted-foreground">{group.records.length}</span>
            </button>
          )}
          {!collapsed.has(group.key) && (
            <div
              className={cn(
                board && definition.display.group_by === "none"
                  ? "grid gap-3 sm:grid-cols-2 xl:grid-cols-3"
                  : board
                    ? "space-y-2"
                    : "divide-y divide-border/40",
              )}
            >
              {group.records.map((record) => (
                <button
                  key={record.id}
                  type="button"
                  onClick={() => onOpen(record)}
                  className={cn(
                    "w-full text-left transition-colors focus-visible:outline-2 focus-visible:outline-ring",
                    board
                      ? "flex flex-col gap-4 rounded-xl border border-border/70 bg-background p-4 shadow-sm hover:bg-accent/40"
                      : "flex flex-wrap items-center gap-3 rounded-md px-3 py-3 hover:bg-muted/50",
                  )}
                >
                  <div className="flex min-w-0 flex-1 items-start gap-2.5">
                    {definition.entity_type === "projects" ? (
                      <FolderIcon className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
                    ) : definition.entity_type === "sessions" ? (
                      <MessageSquareIcon className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
                    ) : (
                      <StatusIcon status={record.status} />
                    )}
                    <span
                      className={cn("text-sm font-medium", board ? "line-clamp-3" : "truncate")}
                    >
                      {record.title}
                    </span>
                  </div>
                  <RecordProperties record={record} definition={definition} />
                </button>
              ))}
              {group.records.length === 0 && (
                <p className="px-3 py-5 text-center text-xs text-muted-foreground">No items</p>
              )}
            </div>
          )}
        </section>
      ))}
    </div>
  );
}

export function ResultsFooter({
  count,
  hasMore,
  loading,
  onMore,
}: {
  count: number;
  hasMore: boolean;
  loading: boolean;
  onMore: () => void;
}) {
  return (
    <div className="flex items-center justify-center gap-4 py-4 text-xs text-muted-foreground">
      <span>
        {count} {count === 1 ? "item" : "items"}
        {hasMore ? " loaded" : ""}
      </span>
      {hasMore && (
        <Button variant="outline" size="sm" disabled={loading} onClick={onMore}>
          {loading ? "Loading…" : "Load more"}
        </Button>
      )}
    </div>
  );
}
