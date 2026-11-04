import { useEffect, useRef, useState } from "react";
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
  | {
      status: "ready";
      prompts: RewindPrompt[];
      olderCursor: string | null;
      /** Loading the next older batch, or why it failed. */
      older: "idle" | "loading" | { error: string };
    }
  | { status: "error"; message: string };

const loadErrorMessage = (err: unknown) =>
  err instanceof Error ? err.message : "Could not load prompts";

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
  // Aborts the open picker's requests, older batches included, when it closes.
  const controllerRef = useRef<AbortController | null>(null);

  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    controllerRef.current = controller;
    setState({ status: "loading" });
    fetchRewindPrompts(sessionId, controller.signal).then(
      ({ prompts, olderCursor }) => {
        // A closed or switched picker drops replies meant for its earlier opening.
        if (controller.signal.aborted) return;
        setState({ status: "ready", prompts, olderCursor, older: "idle" });
      },
      (err: unknown) => {
        if (controller.signal.aborted) return;
        setState({ status: "error", message: loadErrorMessage(err) });
      },
    );
    return () => controller.abort();
  }, [open, sessionId]);

  const loadOlder = () => {
    const controller = controllerRef.current;
    if (state.status !== "ready" || state.olderCursor === null || state.older === "loading") return;
    if (controller === null) return;
    setState({ ...state, older: "loading" });
    fetchRewindPrompts(sessionId, controller.signal, state.olderCursor).then(
      (page) => {
        if (controller.signal.aborted) return;
        setState((current) =>
          current.status === "ready"
            ? {
                status: "ready",
                prompts: [...current.prompts, ...page.prompts],
                olderCursor: page.olderCursor,
                older: "idle",
              }
            : current,
        );
      },
      (err: unknown) => {
        if (controller.signal.aborted) return;
        setState((current) =>
          current.status === "ready"
            ? { ...current, older: { error: loadErrorMessage(err) } }
            : current,
        );
      },
    );
  };

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
                {state.olderCursor !== null && (
                  // Shown whatever the search, so an older prompt stays reachable.
                  <CommandItem
                    forceMount
                    value="load older prompts"
                    disabled={state.older === "loading"}
                    onSelect={loadOlder}
                    data-testid="rewind-load-older"
                    className="text-muted-foreground"
                  >
                    {state.older === "loading"
                      ? "Loading older prompts…"
                      : typeof state.older === "object"
                        ? `${state.older.error} — try again`
                        : "Load older prompts"}
                  </CommandItem>
                )}
              </>
            )}
          </CommandList>
        </Command>
      </DialogContent>
    </Dialog>
  );
}
