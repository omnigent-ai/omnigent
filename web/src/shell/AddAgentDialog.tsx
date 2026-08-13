import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "@/lib/routing";
import { useQueryClient } from "@tanstack/react-query";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { AgentCard } from "@/components/AgentCard";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import { childSessionsQueryKey, useChildSessions } from "@/hooks/useChildSessions";
import { createSession } from "@/lib/sessionsApi";
import { setPendingInitialPrompt } from "@/store/chatStore";
import { composeAskSubagentPrompt, sanitizeInitialPrompt } from "@/lib/initialPrompt";
import { deriveSubagentName, uniqueSubagentName } from "@/lib/subagentName";
import type { AskSubagentSelection } from "@/shell/AskSubagentContext";

// Title sentinel marking a user-added agent. Mirrors the server's
// ``_UI_ADDED_AGENT_TITLE_PREFIX`` in omnigent/server/routes/sessions.py:
// the 3-segment "ui:<agent>:<name>" title is parsed back into
// tool=<agent>, session_name=<name> by the child_sessions endpoint, and a
// sub-agent named "ui" is rejected by the spec validator to avoid collision.
const UI_ADDED_TITLE_PREFIX = "ui";

/**
 * "Add agent" picker for the Agents rail.
 *
 * Lets the user attach a new agent (Claude Code, codex, a registered
 * custom agent) as a child of the active session. On submit it creates a
 * child session via ``POST /v1/sessions`` with ``parent_session_id`` set
 * and ``sub_agent_name`` left null (so the runner resolves the child's own
 * bound agent rather than a parent sub-spec), queues the required first task
 * (``setPendingInitialPrompt``), then navigates into the new child — whose
 * bind flow sends that task exactly once, so it starts working without the
 * user retyping. The agent catalog is the same ``GET /v1/agents`` list the
 * new-session picker uses.
 *
 * When ``initialContext`` is set (the "Ask sub-agent" flow — text selected in a
 * chat response), the dialog shows that text as read-only context, relabels the
 * task field to "Question", prefills the child's name from the selection
 * (unique across the root tree, editable), and delivers the selected context +
 * question as one composed prompt on submit.
 *
 * @param parentSessionId - Session the new agent is added under.
 * @param open - Whether the dialog is visible.
 * @param onOpenChange - Visibility setter (Radix-controlled).
 * @param initialContext - Selected chat text to include as context; omitted for
 *   the manual Add-agent flow.
 * @param existingNames - Sub-agent names already present in the root tree, so
 *   the Ask flow's derived name can be disambiguated; omitted for the manual
 *   flow.
 */
export function AddAgentDialog({
  parentSessionId,
  open,
  onOpenChange,
  initialContext,
  existingNames,
}: {
  parentSessionId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  initialContext?: AskSubagentSelection;
  existingNames?: readonly string[];
}) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { data: agents } = useAvailableAgents();

  const agentList = agents ?? [];
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [task, setTask] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const askSelection = initialContext ?? null;
  const isAsk = askSelection !== null;

  const [nameEdited, setNameEdited] = useState(false);

  // Names already taken: fresh direct siblings (the server enforces per-parent
  // title uniqueness) unioned with the root-tree names from the rail cache, so
  // the Ask flow's derived name is unique across the tree, not just siblings.
  const { children: existingChildren } = useChildSessions(parentSessionId);
  const takenNames = useMemo(() => {
    const siblings = existingChildren
      .map((child) => child.session_name)
      .filter((n): n is string => typeof n === "string" && n.length > 0);
    return [...siblings, ...(existingNames ?? [])];
  }, [existingChildren, existingNames]);

  // Selection-derived, collision-free default name for the Ask flow.
  const derivedAskName = useMemo(
    () =>
      askSelection === null
        ? ""
        : uniqueSubagentName(deriveSubagentName(askSelection.selectedText), takenNames),
    [askSelection, takenNames],
  );

  // On open: clear the question + edit flag (the derived name is applied below).
  useEffect(() => {
    if (open) {
      setNameEdited(false);
      setTask("");
      setError(null);
    }
  }, [open]);
  // Keep the name synced to the derived default until the user edits it, so
  // late-arriving tree names can disambiguate (Snappy → Snappy (2)).
  useEffect(() => {
    if (open && isAsk && !nameEdited) setName(derivedAskName);
  }, [open, isAsk, nameEdited, derivedAskName]);

  const selectedAgent = agentList.find((a) => a.id === selectedAgentId) ?? null;

  function selectAgent(agentId: string): void {
    setSelectedAgentId(agentId);
    setError(null);
  }

  function handleOpenChange(next: boolean): void {
    if (!next) {
      setSelectedAgentId(null);
      setName("");
      setTask("");
      setError(null);
      setSubmitting(false);
      setNameEdited(false);
    }
    onOpenChange(next);
  }

  async function handleAdd(): Promise<void> {
    if (selectedAgent === null) return;
    const trimmed = name.trim();
    if (!trimmed) {
      setError("Enter a name for the agent.");
      return;
    }
    // The task is the child's first instruction. Sanitize it the same way the
    // New Chat composer does (strip terminal-corrupting control chars, trim)
    // before queueing, since a native-terminal child receives it via tmux.
    const questionText = sanitizeInitialPrompt(task);
    if (!questionText) {
      setError(isAsk ? "Enter a question for the agent." : "Enter a task for the agent.");
      return;
    }
    const title = `${UI_ADDED_TITLE_PREFIX}:${selectedAgent.name}:${trimmed}`;
    setSubmitting(true);
    setError(null);
    try {
      const session = await createSession(selectedAgent.id, [], {
        parentSessionId,
        subAgentName: null,
        title,
      });
      // Queue the child's first message (consumed once after it binds, keyed by
      // conversation id), then navigate — the same handoff New Chat uses.
      // Ask flow delivers the composed selection + question; manual sends as-is.
      const deliveredText =
        askSelection !== null
          ? composeAskSubagentPrompt(
              sanitizeInitialPrompt(askSelection.selectedText),
              askSelection.surroundingExcerpt !== null
                ? sanitizeInitialPrompt(askSelection.surroundingExcerpt)
                : null,
              questionText,
            )
          : questionText;
      setPendingInitialPrompt(session.id, { text: deliveredText, skill: null });
      // Refresh the rail so the new child appears immediately, then jump into it.
      await queryClient.invalidateQueries({
        queryKey: childSessionsQueryKey(parentSessionId),
      });
      handleOpenChange(false);
      navigate(`/c/${session.id}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't add the agent. Try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent
        data-testid="add-agent-dialog"
        // max-w-lg matches NewChatDialog so the shared AgentCard renders at
        // the same width — and thus the same height once a description wraps.
        className="flex max-h-[85vh] flex-col gap-4 sm:max-w-lg"
      >
        <DialogHeader>
          <DialogTitle>Add agent</DialogTitle>
        </DialogHeader>

        <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto">
          {askSelection !== null && (
            <div className="flex flex-col gap-1.5">
              <span className="text-sm font-medium text-muted-foreground">Selected context</span>
              {/* Read-only view of the highlighted text; delivered (with its
                  surrounding excerpt) as the composed prompt on submit. */}
              <div
                data-testid="add-agent-context"
                className="max-h-32 overflow-y-auto whitespace-pre-wrap rounded-md border border-input bg-muted/40 px-3 py-2 text-sm text-muted-foreground"
              >
                {askSelection.selectedText}
              </div>
            </div>
          )}
          <div className="flex flex-col gap-2">
            <span className="text-sm font-medium text-muted-foreground">Pick an agent</span>
            {agentList.length === 0 ? (
              <p data-testid="add-agent-empty" className="text-sm text-muted-foreground">
                No agents available on this server. Register one with{" "}
                <code className="font-mono">omnigent server --agent</code>.
              </p>
            ) : (
              agentList.map((agent) => (
                <AgentCard
                  key={agent.id}
                  agent={agent}
                  selected={agent.id === selectedAgentId}
                  onSelect={() => selectAgent(agent.id)}
                  hover
                />
              ))
            )}
          </div>

          {selectedAgent !== null && (
            <>
              <div className="flex flex-col gap-1.5">
                <label
                  htmlFor="add-agent-name"
                  className="text-sm font-medium text-muted-foreground"
                >
                  Name
                </label>
                {/* Raw input matching NewChatDialog's "Name" field for a
                    consistent look (rounded-md + border-tint focus, no
                    heavy ring). */}
                <input
                  id="add-agent-name"
                  data-testid="add-agent-name-input"
                  type="text"
                  value={name}
                  onChange={(e) => {
                    setName(e.target.value);
                    setNameEdited(true);
                  }}
                  placeholder="Name this agent"
                  className="rounded-md border border-input bg-background px-3 py-2 font-mono text-sm outline-none transition-colors focus-visible:border-ring"
                />
              </div>

              <div className="flex flex-col gap-1.5">
                <label
                  htmlFor="add-agent-task"
                  className="text-sm font-medium text-muted-foreground"
                >
                  {isAsk ? "Question" : "Task"}
                </label>
                {/* The child's first instruction, delivered once after the
                    child binds (see handleAdd). Multi-line so a longer brief
                    fits; required, so submit stays disabled until it's typed. */}
                <textarea
                  id="add-agent-task"
                  data-testid="add-agent-initial-prompt-input"
                  value={task}
                  onChange={(e) => setTask(e.target.value)}
                  placeholder={
                    isAsk
                      ? "Ask about the selected text…"
                      : "Review the current diff and report correctness issues."
                  }
                  rows={3}
                  className="min-h-16 resize-y rounded-md border border-input bg-background px-3 py-2 text-sm outline-none transition-colors focus-visible:border-ring"
                />
              </div>
            </>
          )}

          {error !== null && (
            <p data-testid="add-agent-error" className="text-sm text-destructive">
              {error}
            </p>
          )}
        </div>

        <DialogFooter>
          <Button variant="ghost" onClick={() => handleOpenChange(false)} disabled={submitting}>
            Cancel
          </Button>
          <Button
            data-testid="add-agent-submit"
            onClick={handleAdd}
            loading={submitting}
            disabled={selectedAgent === null || !name.trim() || !task.trim()}
          >
            Add
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
