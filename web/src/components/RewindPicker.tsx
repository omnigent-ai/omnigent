import { useEffect, useState } from "react";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { fetchRewindPrompts, type RewindPrompt } from "@/lib/sessionsApi";

type PromptsState =
  | { status: "loading" }
  | { status: "ready"; prompts: RewindPrompt[] }
  | { status: "error"; message: string };

interface RewindPickerProps {
  /** Session whose prompts are listed, e.g. "conv_abc". */
  sessionId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Called with the prompt the user chose to rewind to. */
  onPick: (prompt: RewindPrompt) => void;
}

/**
 * The `/rewind` picker: the session's prompts, newest first, searchable.
 * Choosing one rewinds the conversation to just before it.
 */
export function RewindPicker({ sessionId, open, onOpenChange, onPick }: RewindPickerProps) {
  const [state, setState] = useState<PromptsState>({ status: "loading" });

  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    setState({ status: "loading" });
    fetchRewindPrompts(sessionId, controller.signal).then(
      (prompts) => setState({ status: "ready", prompts }),
      (err: unknown) => {
        if (controller.signal.aborted) return;
        setState({
          status: "error",
          message: err instanceof Error ? err.message : "Could not load prompts",
        });
      },
    );
    return () => controller.abort();
  }, [open, sessionId]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="top-1/4 translate-y-0 overflow-hidden rounded-xl p-0 sm:max-w-lg"
        showCloseButton={false}
        data-testid="rewind-picker"
      >
        <DialogTitle className="sr-only">Rewind conversation</DialogTitle>
        <DialogDescription className="sr-only">
          Choose a prompt. The conversation continues in a new chat from just before it, with the
          prompt ready to edit.
        </DialogDescription>
        <Command label="Rewind to a prompt" loop>
          <CommandInput placeholder="Rewind to a prompt…" />
          <CommandList>
            {state.status === "loading" && (
              <div className="px-3 py-6 text-center text-sm text-muted-foreground">
                Loading prompts…
              </div>
            )}
            {state.status === "error" && (
              <div role="alert" className="px-3 py-6 text-center text-sm text-destructive">
                {state.message}
              </div>
            )}
            {state.status === "ready" && (
              <>
                <CommandEmpty>No prompts to rewind to</CommandEmpty>
                <CommandGroup heading="Continue from just before">
                  {state.prompts.map((prompt) => (
                    <CommandItem
                      key={prompt.itemId}
                      value={`${prompt.text} ${prompt.itemId}`}
                      onSelect={() => onPick(prompt)}
                      data-testid="rewind-prompt"
                    >
                      <span className="min-w-0 flex-1 truncate text-left">
                        {prompt.text.trim() || "(attachment)"}
                      </span>
                    </CommandItem>
                  ))}
                </CommandGroup>
              </>
            )}
          </CommandList>
        </Command>
      </DialogContent>
    </Dialog>
  );
}
