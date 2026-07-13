// Structured card for a workflow node's terminal ``<workflow_result>{...}</workflow_result>``
// block. Child agents in a Polly DAG must end with that JSON envelope, which
// otherwise dumps raw into the transcript. This renders the common fields —
// summary, PR link, branch, files changed, and gate results — at a glance,
// with the full JSON one chevron away for anything unrecognized.

import { useMemo, useState } from "react";
import {
  CheckIcon,
  ChevronRightIcon,
  ExternalLinkIcon,
  FileIcon,
  GitBranchIcon,
  XIcon,
} from "lucide-react";
import { CodeBlock, CodeBlockHeader, CodeBlockTitle } from "@/components/ai-elements/code-block";
import { Badge } from "@/components/ui/badge";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { cn } from "@/lib/utils";

/** Matches the last ``<workflow_result>{...}</workflow_result>`` block. */
const WORKFLOW_RESULT_RE = /<workflow_result>\s*(\{[\s\S]*?\})\s*<\/workflow_result>/g;

export interface ParsedWorkflowResult {
  /** Text before the result block (agent prose), trimmed. */
  before: string;
  /** Text after the result block, trimmed. */
  after: string;
  /** The parsed result object. */
  result: Record<string, unknown>;
  /** The raw JSON string, for the collapsible. */
  raw: string;
}

/**
 * Extract a ``<workflow_result>`` JSON block from message text.
 *
 * Returns ``null`` when there is no block or its JSON is unparseable — callers
 * then fall back to plain markdown so a malformed block is never swallowed.
 */
export function parseWorkflowResult(text: string): ParsedWorkflowResult | null {
  const matches = [...text.matchAll(WORKFLOW_RESULT_RE)];
  if (matches.length === 0) return null;
  const match = matches[matches.length - 1]!;
  let result: unknown;
  try {
    result = JSON.parse(match[1]!);
  } catch {
    return null;
  }
  if (typeof result !== "object" || result === null || Array.isArray(result)) return null;
  const start = match.index ?? 0;
  return {
    before: text.slice(0, start).trim(),
    after: text.slice(start + match[0].length).trim(),
    result: result as Record<string, unknown>,
    raw: match[1]!,
  };
}

function asString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function asStringArray(value: unknown): string[] | null {
  if (!Array.isArray(value)) return null;
  const strings = value.filter((v): v is string => typeof v === "string");
  return strings.length ? strings : null;
}

/** A gate whose value starts with "pass" reads as passed. */
function gateEntries(value: unknown): { name: string; passed: boolean; detail: string }[] | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  const entries = Object.entries(value as Record<string, unknown>).map(([name, raw]) => {
    const detail = typeof raw === "string" ? raw : JSON.stringify(raw);
    return { name, passed: /^\s*pass/i.test(detail), detail };
  });
  return entries.length ? entries : null;
}

// Fields rendered explicitly; the rest fall into the raw JSON view.
const KNOWN_KEYS = new Set(["summary", "pr_url", "branch", "files_changed", "gates"]);

export function WorkflowResultCard({ result, raw }: { result: Record<string, unknown>; raw: string }) {
  const [open, setOpen] = useState(false);
  const summary = asString(result.summary);
  const prUrl = asString(result.pr_url);
  const branch = asString(result.branch);
  const files = asStringArray(result.files_changed);
  const gates = gateEntries(result.gates);
  const hasExtraKeys = useMemo(
    () => Object.keys(result).some((k) => !KNOWN_KEYS.has(k)),
    [result],
  );

  return (
    <div className="rounded-lg border bg-card p-3 text-sm shadow-sm">
      <div className="mb-2 flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
        <CheckIcon className="size-3.5 text-success" />
        Workflow result
      </div>

      {summary && <p className="mb-2 text-foreground">{summary}</p>}

      <div className="flex flex-col gap-1.5">
        {prUrl && (
          <a
            href={prUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex w-fit items-center gap-1 text-xs font-medium text-brand-accent hover:underline"
          >
            <ExternalLinkIcon className="size-3.5" />
            {prUrl.replace(/^https?:\/\/github\.com\//, "")}
          </a>
        )}
        {branch && (
          <div className="inline-flex items-center gap-1 text-xs text-muted-foreground">
            <GitBranchIcon className="size-3.5" />
            <code className="rounded bg-muted px-1 py-0.5 font-mono">{branch}</code>
          </div>
        )}
      </div>

      {gates && (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {gates.map((gate) => (
            <Badge
              key={gate.name}
              variant="outline"
              title={gate.detail}
              className={cn(
                "gap-1 text-[10px]",
                gate.passed ? "border-success/50 text-success" : "border-destructive/50 text-destructive",
              )}
            >
              {gate.passed ? <CheckIcon className="size-3" /> : <XIcon className="size-3" />}
              {gate.name}
            </Badge>
          ))}
        </div>
      )}

      {files && (
        <details className="mt-2 text-xs">
          <summary className="cursor-pointer text-muted-foreground hover:text-foreground">
            {files.length} file{files.length === 1 ? "" : "s"} changed
          </summary>
          <ul className="mt-1 flex flex-col gap-0.5 pl-1">
            {files.map((file) => (
              <li key={file} className="flex items-center gap-1 font-mono text-[11px]">
                <FileIcon className="size-3 shrink-0 text-muted-foreground" />
                <span className="truncate">{file}</span>
              </li>
            ))}
          </ul>
        </details>
      )}

      {(hasExtraKeys || !summary) && (
        <Collapsible open={open} onOpenChange={setOpen} className="mt-2">
          <CollapsibleTrigger className="inline-flex items-center gap-1 text-[11px] text-muted-foreground hover:text-foreground">
            <ChevronRightIcon
              className={cn("size-3.5 transition-transform", open && "rotate-90")}
            />
            Raw result
          </CollapsibleTrigger>
          <CollapsibleContent>
            <CodeBlock code={raw} language="json" className="mt-1">
              <CodeBlockHeader>
                <CodeBlockTitle>workflow_result</CodeBlockTitle>
              </CodeBlockHeader>
            </CodeBlock>
          </CollapsibleContent>
        </Collapsible>
      )}
    </div>
  );
}
