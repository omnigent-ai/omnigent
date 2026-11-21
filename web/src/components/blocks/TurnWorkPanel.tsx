// "Show your work" panel under an assistant answer: the turn's queries (SQL,
// shell and code tool calls) and the data sources its calls name, each with
// the call's full input and result one click away.

import {
  ChevronRightIcon,
  Code2Icon,
  DatabaseIcon,
  FileTextIcon,
  GlobeIcon,
  SearchIcon,
  TableIcon,
  TerminalIcon,
  XIcon,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useMemo } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import type { CodeLanguage } from "@/components/ai-elements/code-block";
import { resolveChatFilePath } from "@/hooks/useWorkspaceChangedFiles";
import { mcpServerDisplayName } from "@/lib/mcpServerName";
import type { RenderItem } from "@/lib/renderItems";
import { formatToolTitle } from "@/lib/toolTitle";
import {
  collectTurnWork,
  type ToolRenderItem,
  type TurnCallStatus,
  type TurnQuery,
  type TurnQueryLanguage,
  type TurnSource,
  type TurnSourceKind,
  type TurnWork,
} from "@/lib/turnWork";
import { ExternalPreviewLink } from "@/shell/ExternalPreviewLink";
import { useFileViewer, useWorkspacePaths } from "@/shell/FileViewerContext";
import { formatToolDuration, ToolCallDetails } from "./ToolCard";
import { CodePanel, ToolErrorBox } from "./ToolCodePanel";

const LANGUAGE: Record<TurnQueryLanguage, { label: string; code: CodeLanguage; icon: LucideIcon }> =
  {
    sql: { label: "SQL", code: "sql", icon: DatabaseIcon },
    shell: { label: "Shell", code: "bash", icon: TerminalIcon },
    python: { label: "Python", code: "python", icon: Code2Icon },
    code: { label: "Code", code: "text", icon: Code2Icon },
  };

const SOURCE_ICON: Record<TurnSourceKind, LucideIcon> = {
  file: FileTextIcon,
  table: TableIcon,
  url: GlobeIcon,
  web_search: SearchIcon,
};

const SOURCE_KIND_LABEL: Record<TurnSourceKind, string> = {
  file: "File",
  table: "Table",
  url: "URL",
  web_search: "Web search",
};

const STATUS: Record<TurnCallStatus, { label: string; className: string }> = {
  completed: { label: "Completed", className: "text-muted-foreground" },
  failed: { label: "Failed", className: "text-destructive" },
  running: { label: "Running", className: "text-info" },
  cancelled: { label: "Cancelled", className: "text-muted-foreground" },
  "no-output": { label: "No output recorded", className: "text-muted-foreground" },
};

export function TurnWorkPanel({
  id,
  items,
  onClose,
}: {
  id?: string;
  /** The answer's render items; read only while the panel is open. */
  items: readonly RenderItem[];
  onClose: () => void;
}) {
  const work = useMemo(() => collectTurnWork(items), [items]);
  const empty = work.queries.length === 0 && work.sources.length === 0;
  return (
    <section
      id={id}
      aria-label="Show your work"
      data-testid="turn-work-panel"
      className="not-prose w-full space-y-3 rounded-lg border bg-card/40 p-3 text-sm"
    >
      <header className="flex items-center gap-2">
        <h3 className="font-medium text-foreground text-ui">Show your work</h3>
        <span className="text-muted-foreground">{summaryLine(work)}</span>
        <Button
          aria-label="Close sources"
          className="ml-auto size-6 text-muted-foreground"
          onClick={onClose}
          size="icon-xs"
          type="button"
          variant="ghost"
          componentId="chat.message.sources_close"
        >
          <XIcon className="size-3.5" />
        </Button>
      </header>
      {empty && (
        <p className="text-muted-foreground">
          This answer&rsquo;s calls finished without reporting data they read.
        </p>
      )}
      {work.queries.length > 0 && (
        <div className="space-y-2">
          <h4 className="font-medium text-muted-foreground">Queries</h4>
          <ol className="space-y-3">
            {work.queries.map((query, index) => (
              <li key={query.key}>
                <QueryEntry query={query} position={index + 1} />
              </li>
            ))}
          </ol>
        </div>
      )}
      {work.sources.length > 0 && (
        <div className="space-y-2">
          <h4 className="font-medium text-muted-foreground">Data sources</h4>
          <ul className="space-y-1">
            {work.sources.map((source) => (
              <li key={source.key}>
                <SourceEntry source={source} />
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

function summaryLine({ queries, sources }: TurnWork): string {
  const parts: string[] = [];
  if (queries.length > 0)
    parts.push(`${queries.length} ${queries.length === 1 ? "query" : "queries"}`);
  if (sources.length > 0) {
    parts.push(`${sources.length} ${sources.length === 1 ? "source" : "sources"}`);
  }
  return parts.join(" · ");
}

function QueryEntry({ query, position }: { query: TurnQuery; position: number }) {
  const language = LANGUAGE[query.language];
  const Icon = language.icon;
  return (
    <article data-testid="turn-work-query" className="space-y-1.5">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-muted-foreground">
        <Icon className="size-3.5 shrink-0" aria-hidden />
        <span className="font-medium text-foreground">{query.label}</span>
        {query.server !== undefined && (
          <Badge
            variant="outline"
            className="h-[1.15rem] shrink-0 rounded px-1 font-normal text-muted-foreground text-sm"
          >
            {mcpServerDisplayName(query.server)}
          </Badge>
        )}
        {query.status !== null && (
          <span className={STATUS[query.status].className}>{STATUS[query.status].label}</span>
        )}
        {query.resultSize !== null && <span>{query.resultSize}</span>}
        {query.durationS !== undefined && (
          <span className="tabular-nums">{formatToolDuration(query.durationS)}</span>
        )}
      </div>
      <CodePanel
        title={language.label}
        text={query.text}
        copyText={query.text}
        copyLabel={`Copy query ${position}`}
        language={language.code}
      />
      {query.dataSource !== null && (
        <p className="text-muted-foreground">
          Source: <span className="font-mono text-foreground">{query.dataSource}</span>
        </p>
      )}
      {query.error !== null && <ToolErrorBox>{query.error}</ToolErrorBox>}
      <ToolCallDisclosure item={query.item} name={`Full tool call for query ${position}`} />
    </article>
  );
}

/** The call's input and full result, collapsed until asked for. */
function ToolCallDisclosure({ item, name }: { item: ToolRenderItem; name: string }) {
  return (
    <Collapsible className="group/work-call">
      <CollapsibleTrigger
        aria-label={name}
        className="flex cursor-pointer items-center gap-1 text-muted-foreground transition-colors hover:text-foreground mobile-touch-target"
      >
        <ChevronRightIcon className="size-3.5 shrink-0 transition-transform group-data-[state=open]/work-call:rotate-90" />
        Full tool call
      </CollapsibleTrigger>
      <CollapsibleContent className="mt-1 ml-2 border-l py-1 pl-3">
        <ToolCallDetails item={item} />
      </CollapsibleContent>
    </Collapsible>
  );
}

function SourceEntry({ source }: { source: TurnSource }) {
  const Icon = SOURCE_ICON[source.kind];
  const tools = [...new Set(source.items.map(callTitle))];
  const calls = source.items.length;
  return (
    <div data-testid="turn-work-source" className="flex min-w-0 items-center gap-2">
      <Icon
        className="size-3.5 shrink-0 text-muted-foreground"
        aria-label={SOURCE_KIND_LABEL[source.kind]}
      />
      <span className="min-w-0 truncate">
        <SourceValue source={source} />
      </span>
      <span className="shrink-0 text-muted-foreground">
        {tools.join(", ")}
        {calls > 1 && ` ×${calls}`}
      </span>
    </div>
  );
}

function SourceValue({ source }: { source: TurnSource }) {
  const openFile = useFileViewer();
  const { root, home } = useWorkspacePaths();
  if (source.kind === "url") {
    return (
      <ExternalPreviewLink
        href={source.value}
        className="underline decoration-dotted underline-offset-2 hover:text-foreground"
      >
        {source.value}
      </ExternalPreviewLink>
    );
  }
  if (source.kind === "file") {
    const path = resolveChatFilePath(source.value, root, home)?.path ?? null;
    if (openFile === null || path === null) {
      return <span className="font-mono">{path ?? source.value}</span>;
    }
    return (
      <button
        type="button"
        className="cursor-pointer font-mono underline decoration-dotted underline-offset-2 transition-colors hover:text-foreground"
        onClick={() => openFile(path)}
      >
        {path}
      </button>
    );
  }
  if (source.kind === "web_search") return <span>&ldquo;{source.value}&rdquo;</span>;
  return <span className="font-mono">{source.value}</span>;
}

/** A call's tool label for a source row ("Read", "Web fetch:"). */
function callTitle(item: ToolRenderItem): string {
  if (item.kind === "native_tool") return item.label;
  const title = formatToolTitle(item.execution.name, item.execution.arguments);
  return (title.verb ?? "Command").replace(/:$/, "");
}
