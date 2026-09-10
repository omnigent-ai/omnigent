import { ChevronRightIcon, FileSearchCorner } from "lucide-react";
import { useMemo } from "react";
import { CodeBlock, CodeBlockHeader, CodeBlockTitle } from "@/components/ai-elements/code-block";
import { Shimmer } from "@/components/ai-elements/shimmer";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import type { ToolState } from "@/lib/renderItems";
import { cn } from "@/lib/utils";
import { TOOL_SURFACE_WIDTH_CLASS } from "./toolSurface";

export interface ContextSaverRouting {
  primaryModels: "all";
  workerRoute: string;
  workerModelReported: string | null;
  routeProvider: string;
  nonDatabricksSourceSharingAllowed: boolean;
}

export interface ContextSaverResult {
  technique: string;
  routing: ContextSaverRouting;
  failure: string | null;
}

function isNullableNonEmptyString(value: unknown): value is string | null {
  return value === null || (typeof value === "string" && value.length > 0);
}

function parsePayload(payload: unknown): ContextSaverResult | null {
  if (Array.isArray(payload)) {
    const textBlock = payload.find(
      (block): block is { type: string; text: string } =>
        typeof block === "object" &&
        block !== null &&
        (block as Record<string, unknown>).type === "text" &&
        typeof (block as Record<string, unknown>).text === "string",
    );
    return textBlock ? parseContextSaverResult(textBlock.text) : null;
  }
  if (typeof payload !== "object" || payload === null) return null;

  const result = payload as Record<string, unknown>;
  const rawRouting = result.model_routing;
  if (typeof rawRouting !== "object" || rawRouting === null) return null;
  const routing = rawRouting as Record<string, unknown>;
  const primaryModels = routing.primary_models;
  const workerRoute = routing.worker_route;
  const workerModelReported = routing.worker_model_reported;
  const routeProvider = routing.route_provider;
  const nonDatabricksSourceSharingAllowed = routing.non_databricks_source_sharing_allowed;
  const technique = result.technique;
  const failure = result.failure;

  if (
    primaryModels !== "all" ||
    typeof workerRoute !== "string" ||
    workerRoute.length === 0 ||
    !isNullableNonEmptyString(workerModelReported) ||
    typeof routeProvider !== "string" ||
    routeProvider.length === 0 ||
    typeof nonDatabricksSourceSharingAllowed !== "boolean" ||
    typeof technique !== "string" ||
    !(failure === null || typeof failure === "string")
  ) {
    return null;
  }

  return {
    technique,
    routing: {
      primaryModels,
      workerRoute,
      workerModelReported,
      routeProvider,
      nonDatabricksSourceSharingAllowed,
    },
    failure,
  };
}

/** Parse raw or Claude-SDK-wrapped ``sys_context_read`` output. */
export function parseContextSaverResult(output: string): ContextSaverResult | null {
  try {
    return parsePayload(JSON.parse(output));
  } catch {
    return null;
  }
}

interface ContextSaverCardProps {
  output: string | null;
  state: ToolState;
}

/** Show the model route used for a Focused Read without exposing credentials. */
export function ContextSaverCard({ output, state }: ContextSaverCardProps) {
  const result = useMemo(
    () => (output === null ? null : parseContextSaverResult(output)),
    [output],
  );
  const running = state === "input-available";
  const failed =
    !running && (state !== "output-available" || result === null || result.failure !== null);
  const prettyOutput = useMemo(() => {
    if (output === null) return null;
    try {
      return JSON.stringify(JSON.parse(output), null, 2);
    } catch {
      return output;
    }
  }, [output]);

  return (
    <Collapsible
      defaultOpen={false}
      className={cn(
        "group not-prose my-1 flex flex-col gap-1.5 rounded-md border border-border bg-muted/30 px-3 py-2",
        TOOL_SURFACE_WIDTH_CLASS,
      )}
      data-testid="context-saver-card"
      data-state-kind={running ? "running" : failed ? "failed" : "complete"}
    >
      <div className="flex items-center gap-1.5 text-sm">
        <FileSearchCorner className="size-3.5 shrink-0 text-muted-foreground" />
        <span className="font-medium">Context Saver</span>
        {running ? (
          <Shimmer as="span" className="text-sm">
            Running Focused Read…
          </Shimmer>
        ) : (
          <span className="text-muted-foreground">
            {failed ? "· Focused Read failed" : "· Focused Read complete"}
          </span>
        )}
        {prettyOutput !== null && (
          <CollapsibleTrigger
            className="ml-auto cursor-pointer rounded p-0.5 text-muted-foreground hover:text-foreground"
            aria-label="Show raw Context Saver response"
            data-testid="context-saver-raw-toggle"
          >
            <ChevronRightIcon className="size-3 transition-transform group-data-[state=open]:rotate-90" />
          </CollapsibleTrigger>
        )}
      </div>

      {result !== null && (
        <>
          <div className="flex min-w-0 items-center gap-2 text-sm">
            <span className="min-w-0 truncate font-mono text-muted-foreground">
              All primary models
            </span>
            <span className="shrink-0 text-muted-foreground">→</span>
            <span className="min-w-0 break-all rounded-full border border-border bg-muted px-2 py-0.5 font-mono text-xs text-foreground">
              {result.routing.workerRoute}
            </span>
          </div>
          <p className="text-xs text-muted-foreground">
            Focused Read only — your primary model does not change.
          </p>
          <dl className="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-2 gap-y-0.5 text-xs text-muted-foreground">
            <dt>Route provider</dt>
            <dd className="min-w-0 truncate font-mono text-foreground">
              {result.routing.routeProvider}
            </dd>
            <dt>Provider-reported model</dt>
            <dd className="min-w-0 truncate font-mono text-foreground">
              {result.routing.workerModelReported ?? "Not reported"}
            </dd>
          </dl>
          {result.routing.nonDatabricksSourceSharingAllowed && (
            <p className="text-xs text-amber-700 dark:text-amber-400">
              Non-Databricks source sharing is allowed for this worker route.
            </p>
          )}
          {result.failure !== null && (
            <p className="text-xs text-destructive" data-testid="context-saver-error">
              {result.failure}
            </p>
          )}
        </>
      )}

      {failed && result === null && (
        <p className="text-sm text-muted-foreground" data-testid="context-saver-error">
          {output ?? "No Context Saver result was recorded."}
        </p>
      )}

      {prettyOutput !== null && (
        <CollapsibleContent className="data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 data-[state=closed]:animate-out data-[state=open]:animate-in">
          <CodeBlock code={prettyOutput} language="json">
            <CodeBlockHeader>
              <CodeBlockTitle className="min-w-0">
                <span className="truncate font-medium uppercase tracking-wide">Response</span>
              </CodeBlockTitle>
            </CodeBlockHeader>
          </CodeBlock>
        </CollapsibleContent>
      )}
    </Collapsible>
  );
}
