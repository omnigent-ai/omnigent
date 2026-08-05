import { useCallback, useEffect, useRef, useState } from "react";
import { CheckIcon, TerminalIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { DropdownMenuItem } from "@/components/ui/dropdown-menu";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { copyText } from "@/lib/clipboard";
import { getCliServerUrl } from "@/lib/host";
import { buildResumeCommand } from "@/lib/resumeCommand";
import { showToast } from "@/components/ui/toast";

/** How long the affordance holds its confirmed (check icon) state. */
const COPIED_RESET_MS = 2000;

/**
 * Copy this session's `omnigent resume` command to the clipboard.
 *
 * Shared by the desktop header button and the mobile session menu so the
 * two entry points can't drift apart. Returns `copied` for the
 * confirmed-state icon swap; the toast names the command that landed on
 * the clipboard, which the icon alone can't convey.
 */
export function useCopyResumeCommand(conversationId: string): {
  copied: boolean;
  copyResumeCommand: () => Promise<void>;
} {
  const [copied, setCopied] = useState(false);
  const resetTimeoutRef = useRef<number | null>(null);

  useEffect(() => {
    return () => {
      if (resetTimeoutRef.current !== null) window.clearTimeout(resetTimeoutRef.current);
    };
  }, []);

  const copyResumeCommand = useCallback(async () => {
    const command = buildResumeCommand({
      conversationId,
      serverUrl: getCliServerUrl(),
    });
    try {
      await copyText(command);
    } catch (err) {
      console.warn("Failed to copy resume command", err);
      showToast("Couldn't copy the resume command");
      return;
    }
    setCopied(true);
    if (resetTimeoutRef.current !== null) window.clearTimeout(resetTimeoutRef.current);
    resetTimeoutRef.current = window.setTimeout(() => setCopied(false), COPIED_RESET_MS);
    showToast(
      <span className="flex min-w-0 flex-col gap-0.5">
        <span>Resume command copied</span>
        <code className="truncate font-mono text-xs text-muted-foreground">{command}</code>
      </span>,
      { duration: 4000 },
    );
  }, [conversationId]);

  return { copied, copyResumeCommand };
}

/**
 * Header action that copies this session's `omnigent resume` command, so
 * a session opened in the browser can be picked back up in a terminal.
 *
 * Copy-only: the browser can't launch a local terminal, so handing the
 * user the exact command is the whole affordance.
 */
export function ResumeSessionButton({ conversationId }: { conversationId: string }) {
  const { copied, copyResumeCommand } = useCopyResumeCommand(conversationId);
  const label = copied ? "Resume command copied" : "Copy resume command";
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          aria-label={label}
          data-testid="copy-resume-command"
          onClick={copyResumeCommand}
          className="text-muted-foreground hover:text-foreground"
        >
          {copied ? <CheckIcon className="size-4" /> : <TerminalIcon className="size-4" />}
        </Button>
      </TooltipTrigger>
      {/* Bottom placement matches the header's other tooltips, keeping
          clear of the Electron shell's title-bar strip. */}
      <TooltipContent side="bottom">
        {copied ? label : "Copy `omnigent resume` command"}
      </TooltipContent>
    </Tooltip>
  );
}

/**
 * Mobile three-dot menu counterpart of {@link ResumeSessionButton}.
 *
 * A phone rarely has the terminal that would consume the command, but it
 * may well be the device the user is reading the session on — copying
 * here lets them paste it into a note or a remote shell app.
 */
export function ResumeSessionMenuItem({ conversationId }: { conversationId: string }) {
  const { copyResumeCommand } = useCopyResumeCommand(conversationId);
  return (
    <DropdownMenuItem
      onSelect={copyResumeCommand}
      data-testid="mobile-copy-resume-command"
      className="gap-2.5 px-2.5 py-2 text-base"
    >
      <TerminalIcon className="size-4" />
      Copy resume command
    </DropdownMenuItem>
  );
}
