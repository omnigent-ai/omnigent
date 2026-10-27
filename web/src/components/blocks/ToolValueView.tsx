// Renders any JSON value from a tool call as labeled fields: objects as a
// key/value list, lists of records as rows with a title, detail and status
// badge, and multi-line strings with their real line breaks.

import type { ReactNode } from "react";
import { Badge } from "@/components/ui/badge";
import { absoluteTime } from "@/lib/relativeTime";
import {
  type StatusTone,
  humanizeKey,
  isRecord,
  isoTimestampMs,
  pickDetailField,
  pickStatusField,
  pickTitleField,
  statusLabel,
  statusTone,
} from "@/lib/toolValue";
import { cn } from "@/lib/utils";

// Past this depth a value is shown as compact JSON instead of nested lists.
const MAX_FIELD_DEPTH = 3;
const MAX_LIST_ROWS = 50;
const MAX_INLINE_CHIPS = 20;
const INLINE_CHIP_MAX_CHARS = 60;
const INLINE_STRING_MAX_CHARS = 300;
const EXTRA_ROW_FIELDS = 3;

export function ToolValueView({ value }: { value: unknown }) {
  return (
    <div className="min-w-0 text-sm" data-testid="tool-value-view">
      <ValueNode value={value} depth={0} />
    </div>
  );
}

function ValueNode({ value, depth }: { value: unknown; depth: number }): ReactNode {
  if (value === null || value === undefined) return <EmptyValue />;
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "number") return <span className="tabular-nums">{String(value)}</span>;
  if (typeof value === "string") return <StringValue text={value} />;
  if (Array.isArray(value)) return <ListValue items={value} depth={depth} />;
  if (isRecord(value)) {
    if (depth >= MAX_FIELD_DEPTH) return <CompactJson value={value} />;
    return <FieldList entries={Object.entries(value)} depth={depth} />;
  }
  return String(value);
}

function EmptyValue() {
  return <span className="text-muted-foreground">None</span>;
}

function FieldList({ entries, depth }: { entries: [string, unknown][]; depth: number }) {
  if (entries.length === 0) return <EmptyValue />;
  const shown = entries.slice(0, MAX_LIST_ROWS);
  const hidden = entries.length - shown.length;
  // Only the outermost list is framed as a table; nested values stay plain inside its cells.
  const framed = depth === 0;
  return (
    <>
      <dl
        className={cn(
          "grid grid-cols-[auto_minmax(0,1fr)]",
          framed ? "overflow-hidden rounded-md border" : "gap-x-4 gap-y-1",
        )}
      >
        {shown.map(([key, value]) => (
          <FieldRow key={key} label={humanizeKey(key)} title={key} framed={framed}>
            <ValueNode value={value} depth={depth + 1} />
          </FieldRow>
        ))}
      </dl>
      {hidden > 0 && <p className="mt-1 text-muted-foreground text-sm">and {hidden} more</p>}
    </>
  );
}

function FieldRow({
  label,
  title,
  framed,
  children,
}: {
  label: string;
  title: string;
  framed: boolean;
  children: ReactNode;
}) {
  const term = (
    <dt
      className={cn(
        "max-w-48 break-words text-muted-foreground",
        framed && "bg-muted/30 px-2.5 py-1",
      )}
      title={title}
    >
      {label}
    </dt>
  );
  const detail = <dd className={cn("min-w-0 break-words", framed && "px-2.5 py-1")}>{children}</dd>;
  if (!framed) {
    return (
      <>
        {term}
        {detail}
      </>
    );
  }
  return (
    // The row spans both columns so its divider runs under the label and the value.
    <div className="col-span-2 grid grid-cols-subgrid border-t first:border-t-0">
      {term}
      {detail}
    </div>
  );
}

function StringValue({ text }: { text: string }) {
  if (text.length === 0) return <EmptyValue />;
  if (text.includes("\n") || text.length > INLINE_STRING_MAX_CHARS) {
    return (
      <pre className="max-h-60 overflow-auto whitespace-pre-wrap break-words rounded-md border bg-muted/30 px-2.5 py-1.5 font-mono text-sm">
        {text}
      </pre>
    );
  }
  const ms = isoTimestampMs(text);
  if (ms !== null) {
    return (
      <time dateTime={text} title={text}>
        {absoluteTime(ms)}
      </time>
    );
  }
  if (/^https?:\/\/\S+$/.test(text)) {
    return (
      <a
        href={text}
        target="_blank"
        rel="noopener noreferrer"
        className="break-all text-primary underline-offset-2 hover:underline"
      >
        {text}
      </a>
    );
  }
  // Ids, paths and other single tokens read better in the code face.
  const isToken = !/\s/.test(text) && /[\d/._:-]/.test(text);
  return isToken ? <span className="font-mono text-[0.92em]">{text}</span> : text;
}

function ListValue({ items, depth }: { items: unknown[]; depth: number }) {
  if (items.length === 0) return <EmptyValue />;
  const isChipList =
    items.length <= MAX_INLINE_CHIPS &&
    items.every(
      (item) =>
        typeof item === "number" ||
        (typeof item === "string" && item.length > 0 && item.length <= INLINE_CHIP_MAX_CHARS),
    );
  if (isChipList) {
    return (
      <span className="flex flex-wrap gap-1">
        {items.map((item, index) => (
          // Values can repeat and the list never reorders, so the index is the key.
          // eslint-disable-next-line react/no-array-index-key
          <Badge key={index} variant="outline" className="font-normal text-sm">
            {String(item)}
          </Badge>
        ))}
      </span>
    );
  }
  if (
    depth < MAX_FIELD_DEPTH &&
    items.every(isRecord) &&
    items.some((item) => pickTitleField(item))
  ) {
    return <RecordRows records={items} framed={depth === 0} />;
  }
  if (depth >= MAX_FIELD_DEPTH) return <CompactJson value={items} />;
  return (
    <FieldList
      entries={items.map((item, index): [string, unknown] => [String(index + 1), item])}
      depth={depth}
    />
  );
}

/** A list of records (todos, issues, search hits) as one row each. */
function RecordRows({ records, framed }: { records: Record<string, unknown>[]; framed: boolean }) {
  const shown = records.slice(0, MAX_LIST_ROWS);
  const hidden = records.length - shown.length;
  return (
    <ol className={framed ? "divide-y overflow-hidden rounded-md border" : "grid gap-1.5"}>
      {shown.map((record, index) => (
        // Records carry no guaranteed id and the list never reorders.
        // eslint-disable-next-line react/no-array-index-key
        <RecordRow key={index} record={record} position={index + 1} framed={framed} />
      ))}
      {hidden > 0 && (
        <li className={cn("text-muted-foreground text-sm", framed && "px-2.5 py-1")}>
          and {hidden} more
        </li>
      )}
    </ol>
  );
}

function RecordRow({
  record,
  position,
  framed,
}: {
  record: Record<string, unknown>;
  position: number;
  framed: boolean;
}) {
  const title = pickTitleField(record);
  const detail = pickDetailField(record);
  const status = pickStatusField(record);
  const numericId = typeof record.id === "number" ? record.id : null;
  const shownKeys = new Set([title?.key, detail?.key, status?.key]);
  // A text id ("OMNI-12243") stays with the other fields; only a number is the index.
  if (numericId !== null) shownKeys.add("id");
  const extras = Object.entries(record).filter(
    ([key, value]) =>
      !shownKeys.has(key) && (typeof value === "string" || typeof value === "number"),
  );
  return (
    <li
      className={cn(
        "grid grid-cols-[1.25rem_minmax(0,1fr)_auto] gap-x-2",
        framed && "px-2.5 py-1.5",
      )}
    >
      <span className="text-muted-foreground/70 text-sm tabular-nums leading-[inherit]">
        {numericId ?? position}
      </span>
      <span className="min-w-0 break-words">{title?.value ?? <EmptyValue />}</span>
      {status === null ? <span /> : <StatusBadge status={status.value} />}
      {detail !== null && (
        <span className="col-span-2 col-start-2 text-muted-foreground text-sm leading-snug">
          {detail.value}
        </span>
      )}
      {extras.length > 0 && (
        <span className="col-span-2 col-start-2 truncate text-muted-foreground text-sm">
          {extras.slice(0, EXTRA_ROW_FIELDS).map(([key, value], extraIndex) => (
            <span key={key}>
              {extraIndex > 0 && " · "}
              {humanizeKey(key)} <ExtraValue value={value as string | number} />
            </span>
          ))}
          {extras.length > EXTRA_ROW_FIELDS && ` · +${extras.length - EXTRA_ROW_FIELDS} more`}
        </span>
      )}
    </li>
  );
}

/** A record's extra field on its one-line summary: long or multi-line text collapses. */
function ExtraValue({ value }: { value: string | number }) {
  if (
    typeof value === "number" ||
    (!value.includes("\n") && value.length <= INLINE_CHIP_MAX_CHARS)
  ) {
    return <StringValue text={String(value)} />;
  }
  return value.replace(/\s+/g, " ");
}

const STATUS_TONE_CLASS: Record<StatusTone, string> = {
  success: "border-success/40 bg-success/10 text-success",
  active: "border-info/40 bg-info/10 text-info",
  failure: "border-destructive/40 bg-destructive/10 text-destructive",
  neutral: "text-muted-foreground",
};

function StatusBadge({ status }: { status: string }) {
  return (
    <Badge
      variant="outline"
      className={cn("h-[1.15rem] font-normal text-sm", STATUS_TONE_CLASS[statusTone(status)])}
    >
      {statusLabel(status)}
    </Badge>
  );
}

function CompactJson({ value }: { value: unknown }) {
  return <span className="break-all font-mono text-sm">{JSON.stringify(value)}</span>;
}
