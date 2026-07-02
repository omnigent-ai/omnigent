// Compact card for runner-side terminal commands (!cmd) forwarded from
// the Claude Code embedded TUI transcript. Two variants:
//
//   input  — the command line typed by the user (e.g. `!pwd`). Shows a
//            terminal prompt icon and the raw command text.
//   output — the combined stdout/stderr result. Collapsible so long
//            output doesn't dominate the conversation view.

import { ChevronRightIcon, SquareTerminalIcon } from "lucide-react";
import { CodeBlock, CodeBlockHeader, CodeBlockTitle } from "@/components/ai-elements/code-block";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";

interface TerminalCommandCardProps {
  kind: "input" | "output";
  input: string | null;
  stdout: string | null;
  stderr: string | null;
}

export function TerminalCommandCard({ kind, input, stdout, stderr }: TerminalCommandCardProps) {
  if (kind === "input") {
    return (
      <div
        className="group not-prose w-full"
        data-testid="terminal-command-card"
        data-terminal-kind="input"
      >
        <span className="flex w-full items-center gap-1.5 py-0.5 text-left text-muted-foreground text-xs">
          <SquareTerminalIcon className="size-3 shrink-0 text-emerald-500 dark:text-emerald-400" />
          <span className="min-w-0 flex-1 truncate font-mono">
            <span className="font-semibold text-foreground">$</span> {input ?? ""}
          </span>
        </span>
      </div>
    );
  }

  const hasStdout = typeof stdout === "string" && stdout.length > 0;
  const hasStderr = typeof stderr === "string" && stderr.length > 0;
  const hasOutput = hasStdout || hasStderr;

  const trigger = (
    <span
      className="flex w-full items-center gap-1.5 py-0.5 text-left text-muted-foreground text-xs transition-colors hover:text-foreground"
      data-testid="terminal-command-card"
      data-terminal-kind="output"
    >
      <SquareTerminalIcon className="size-3 shrink-0 text-emerald-500 dark:text-emerald-400" />
      <span className="min-w-0 flex-1 truncate font-mono text-muted-foreground/80">
        {hasOutput ? "output" : "(no output)"}
      </span>
      {hasOutput && (
        <ChevronRightIcon className="size-3 shrink-0 transition-transform group-data-[state=open]:rotate-90" />
      )}
    </span>
  );

  if (!hasOutput) {
    return <div className="group not-prose w-full">{trigger}</div>;
  }

  return (
    <Collapsible defaultOpen={false} className="group not-prose w-full">
      <CollapsibleTrigger className="w-full cursor-pointer">{trigger}</CollapsibleTrigger>
      <CollapsibleContent className="mt-1 ml-2 space-y-2 border-l pl-3 py-1 data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 data-[state=closed]:animate-out data-[state=open]:animate-in">
        {hasStdout && (
          <CodeBlock code={stdout as string} language="bash">
            <CodeBlockHeader>
              <CodeBlockTitle className="min-w-0">
                <span className="truncate font-medium uppercase tracking-wide">stdout</span>
              </CodeBlockTitle>
            </CodeBlockHeader>
          </CodeBlock>
        )}
        {hasStderr && (
          <CodeBlock code={stderr as string} language="bash">
            <CodeBlockHeader>
              <CodeBlockTitle className="min-w-0">
                <span className="truncate font-medium uppercase tracking-wide">stderr</span>
              </CodeBlockTitle>
            </CodeBlockHeader>
          </CodeBlock>
        )}
      </CollapsibleContent>
    </Collapsible>
  );
}
