// Compact "command/skill invoked" indicator for Claude Code slash
// commands observed in the embedded TUI. Reuses ToolCard's row+
// collapsible rhythm. Two kinds:
//
//   skill   — plugin/Skill invocations (``/dev-productivity:simplify``,
//             ``/oncall``). Wand icon in brand pink, prefix "Skill".
//   command — surfaced CLI built-ins (``/effort``, ``/clear``,
//             ``/compact``, ``/model``, ``/ultrareview``). Command
//             icon (⌘ glyph) in slate, prefix "Command". These change
//             conversation state (effort level, context reset,
//             compaction, model swap), so a web observer needs them.

import { ChevronRightIcon, CommandIcon, WandSparklesIcon, type LucideIcon } from "lucide-react";
import { useMemo } from "react";
import { CodeBlock, CodeBlockHeader, CodeBlockTitle } from "@/components/ai-elements/code-block";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";

type SlashCommandKind = "skill" | "command";

interface KindStyle {
  prefix: string;
  Icon: LucideIcon;
  iconClass: string;
}

const KIND_STYLES: Record<SlashCommandKind, KindStyle> = {
  skill: {
    prefix: "Skill",
    // WandSparkles, not Sparkles — plain Sparkles is the thinking/reasoning
    // marker (ai-elements/reasoning), and skills must not look like thoughts.
    Icon: WandSparklesIcon,
    // Brand pink — matches the composer slash-command tint and the nessie
    // workflow chips so every skill surface shares one palette.
    iconClass: "text-brand-accent",
  },
  command: {
    prefix: "Command",
    Icon: CommandIcon,
    iconClass: "text-slate-500 dark:text-slate-400",
  },
};

interface SlashCommandCardProps {
  /** Discriminator: Skill vs surfaced CLI command. Picks prefix + icon. */
  kind: SlashCommandKind;
  /** Command name with leading `/` stripped, e.g. `dev-productivity:simplify`. */
  name: string;
  /** Verbatim `<command-args>` text; empty when none. */
  arguments: string;
  /** `<local-command-stdout>` text, or null when absent (typical for Skills). */
  output: string | null;
}

export function SlashCommandCard({ kind, name, arguments: args, output }: SlashCommandCardProps) {
  const hasArgs = args.length > 0;
  const hasOutput = output !== null && output.length > 0;
  const canExpand = hasArgs || hasOutput;
  const { prefix, Icon, iconClass } = KIND_STYLES[kind];
  // Tooltip keeps the title legible after truncation of long names.
  const tooltip = useMemo(
    () => (hasArgs ? `${prefix} ${name} ${args}` : `${prefix} ${name}`),
    [hasArgs, prefix, name, args],
  );

  const trigger = (
    <span
      title={tooltip}
      className="flex w-full items-center gap-1.5 py-0.5 text-left text-muted-foreground text-xs transition-colors hover:text-foreground"
      data-testid="slash-command-card"
      data-slash-kind={kind}
    >
      <Icon className={`size-3 shrink-0 ${iconClass}`} />
      <span className="min-w-0 flex-1 truncate">
        <span className="font-semibold text-foreground">{prefix}</span> <span>{name}</span>
        {hasArgs && (
          <>
            {" "}
            <span className="text-muted-foreground/80">{args}</span>
          </>
        )}
      </span>
      {canExpand && (
        <ChevronRightIcon className="size-3 shrink-0 transition-transform group-data-[state=open]:rotate-90" />
      )}
    </span>
  );

  // Static row when there's nothing to expand — a chevron-less row
  // that opens an empty panel is a UX papercut.
  if (!canExpand) {
    return <div className="group not-prose w-full">{trigger}</div>;
  }

  return (
    <Collapsible defaultOpen={false} className="group not-prose w-full">
      <CollapsibleTrigger className="w-full cursor-pointer">{trigger}</CollapsibleTrigger>
      <CollapsibleContent className="mt-1 ml-2 space-y-2 border-l pl-3 py-1 data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 data-[state=closed]:animate-out data-[state=open]:animate-in">
        {hasArgs && <ArgsPanel args={args} />}
        {hasOutput && <OutputPanel output={output as string} />}
      </CollapsibleContent>
    </Collapsible>
  );
}

function ArgsPanel({ args }: { args: string }) {
  return (
    <CodeBlock code={args} language="bash">
      <CodeBlockHeader>
        <CodeBlockTitle className="min-w-0">
          <span className="truncate font-medium uppercase tracking-wide">Arguments</span>
        </CodeBlockTitle>
      </CodeBlockHeader>
    </CodeBlock>
  );
}

function OutputPanel({ output }: { output: string }) {
  return (
    <CodeBlock code={output} language="bash">
      <CodeBlockHeader>
        <CodeBlockTitle className="min-w-0">
          <span className="truncate font-medium uppercase tracking-wide">Output</span>
        </CodeBlockTitle>
      </CodeBlockHeader>
    </CodeBlock>
  );
}
