// "New design" dialog: a prompt, the New session agent picker, and an online
// host plus folder. Create sends the same session create the New session
// dialog sends, then the first message, and stays on `/design`.

import { useEffect, useMemo, useRef, useState } from "react";
import { FolderOpenIcon, PaletteIcon, TriangleAlertIcon } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";
import { Label } from "@/components/scheduled/Label";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { useAvailableAgents, type AvailableAgent } from "@/hooks/useAvailableAgents";
import { useHostFilesystem } from "@/hooks/useHostFilesystem";
import { useHosts } from "@/hooks/useHosts";
import { isAcpHarnessAgent, selectableSessionAgents } from "@/lib/agentGrouping";
import { DECK_SUFFIX, deckName } from "@/lib/designDecks";
import {
  deckSlug,
  designDeckPath,
  firstDesignMessage,
  readDesignDefaults,
  rememberDesignDefaults,
} from "@/lib/designStudio";
import { shouldGuardDialogDismiss } from "@/lib/dialogDismissGuard";
import { isNativeCodingAgent, nativeWrapperLabelsForAgent } from "@/lib/nativeCodingAgents";
import { createSession, postEvent } from "@/lib/sessionsApi";
import { DESIGN_KIT_DIR } from "@/shell/codeViewerHelpers";
import { AgentHarnessPicker } from "@/shell/NewChatDialog";
import { WorkspacePickerDialog } from "@/shell/WorkspacePickerDialog";

function joinPath(folder: string, rel: string): string {
  return `${folder.replace(/\/+$/, "")}/${rel}`;
}

export function NewDesignDialog({
  open,
  onOpenChange,
  initialPrompt,
  takenDeckNames,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  initialPrompt?: string;
  /** Deck names the landing already lists for a workspace folder. */
  takenDeckNames: (folder: string) => readonly string[];
  onCreated: (sessionId: string, path: string) => void;
}) {
  const queryClient = useQueryClient();
  const { data: agents } = useAvailableAgents({ enabled: open });
  const { data: hosts } = useHosts({ enabled: open });
  const [prompt, setPrompt] = useState("");
  const [pickedAgentId, setPickedAgentId] = useState<string | null>(null);
  const [pickedHostId, setPickedHostId] = useState<string | null>(null);
  // null: use the remembered folder for the selected host.
  const [pickedFolder, setPickedFolder] = useState<string | null>(null);
  const [browserOpen, setBrowserOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  // A session created by a Create whose first message then failed; a retry
  // with the same agent, host, and folder only resends.
  const created = useRef<{ key: string; id: string } | null>(null);

  const wasOpen = useRef(false);
  useEffect(() => {
    if (open && !wasOpen.current) {
      setPrompt(initialPrompt ?? "");
      setPickedAgentId(null);
      setPickedHostId(null);
      setPickedFolder(null);
      setError(null);
      created.current = null;
    }
    wasOpen.current = open;
  }, [open, initialPrompt]);

  const defaults = useMemo(() => (open ? readDesignDefaults() : {}), [open]);
  const agentList = useMemo(() => selectableSessionAgents(agents ?? []), [agents]);
  const harnessEntries = agentList.filter((a) => isNativeCodingAgent(a) || isAcpHarnessAgent(a));
  const agentEntries = agentList.filter((a) => !isNativeCodingAgent(a) && !isAcpHarnessAgent(a));
  const agentId =
    [pickedAgentId, defaults.agentId].find((id) => agentList.some((a) => a.id === id)) ??
    agentList[0]?.id ??
    null;
  const agent = agentList.find((a) => a.id === agentId);

  const onlineHosts = (hosts ?? []).filter((h) => h.status === "online");
  const host =
    onlineHosts.find((h) => h.host_id === pickedHostId) ??
    onlineHosts.find((h) => h.host_id === defaults.hostId) ??
    onlineHosts[0];
  const hostId = host?.host_id ?? null;
  const folder = pickedFolder ?? (hostId ? (defaults.folders?.[hostId] ?? "") : "");

  const kitListing = useHostFilesystem(hostId, folder ? joinPath(folder, DESIGN_KIT_DIR) : null);
  const decksListing = useHostFilesystem(hostId, folder ? joinPath(folder, "decks") : null);
  const kitHint = kitListing.data?.entries.some((e) => e.name === "kit.json")
    ? "Kit found"
    : kitListing.data || (kitListing.error as { status?: number } | null)?.status === 404
      ? "No kit"
      : null;

  const canCreate =
    prompt.trim() !== "" &&
    agent !== undefined &&
    hostId !== null &&
    folder !== "" &&
    !submitting &&
    !decksListing.isLoading &&
    !decksListing.isPlaceholderData;

  // Nested pickers portal outside the dialog; keep their clicks from closing it.
  const selectOpenCount = useRef(0);
  const selectClosedAt = useRef(0);
  function handleSelectOpenChange(isOpen: boolean) {
    selectOpenCount.current = Math.max(0, selectOpenCount.current + (isOpen ? 1 : -1));
    if (!isOpen) selectClosedAt.current = Date.now();
  }
  function guardDismiss(event: { target: EventTarget | null; preventDefault: () => void }) {
    if (
      browserOpen ||
      shouldGuardDialogDismiss(event.target, {
        selectOpen: selectOpenCount.current > 0,
        msSinceSelectClose: Date.now() - selectClosedAt.current,
      })
    ) {
      event.preventDefault();
    }
  }

  async function handleCreate() {
    if (!canCreate || !agent || !hostId) return;
    setError(null);
    setSubmitting(true);
    try {
      const inFolder = (decksListing.data?.entries ?? [])
        .filter((e) => e.name.endsWith(DECK_SUFFIX))
        .map((e) => deckName(e.name));
      const path = designDeckPath(deckSlug(prompt, [...takenDeckNames(folder), ...inFolder]));
      const key = `${agent.id}\0${hostId}\0${folder}`;
      let sessionId = created.current?.key === key ? created.current.id : null;
      if (sessionId === null) {
        const labels = nativeWrapperLabelsForAgent(agent);
        const session = await createSession(agent.id, [], {
          hostId,
          workspace: folder,
          ...(labels ? { labels } : {}),
        });
        sessionId = session.id;
        created.current = { key, id: sessionId };
      }
      await postEvent(sessionId, {
        type: "message",
        data: {
          role: "user",
          content: [{ type: "input_text", text: firstDesignMessage(prompt, path) }],
        },
      });
      rememberDesignDefaults(agent.id, hostId, folder);
      void queryClient.invalidateQueries({ queryKey: ["conversations"] });
      onCreated(sessionId, path);
      onOpenChange(false);
    } catch (e) {
      setError(e instanceof Error && e.message ? e.message : "Couldn't create the design.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="flex max-h-[90vh] flex-col overflow-hidden p-0 sm:max-w-[560px]"
        data-testid="new-design-dialog"
        onPointerDownOutside={guardDismiss}
        onInteractOutside={guardDismiss}
      >
        <DialogHeader className="shrink-0 px-6 pt-6 pb-0">
          <DialogTitle>New design</DialogTitle>
          <DialogDescription>
            Starts an agent session that builds a slide deck you can watch and refine here.
          </DialogDescription>
        </DialogHeader>

        <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto px-6 py-4">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="design-prompt">Prompt</Label>
            <Textarea
              id="design-prompt"
              value={prompt}
              rows={4}
              required
              placeholder="What should the deck cover?"
              componentId="design.new.prompt"
              className="resize-none text-ui"
              onChange={(e) => setPrompt(e.target.value)}
            />
          </div>

          <div className="flex flex-col gap-1.5">
            <Label>Agent</Label>
            <AgentHarnessPicker
              agentEntries={agentEntries}
              harnessEntries={harnessEntries}
              effectiveAgentId={agentId}
              agentLabel={agent?.display_name ?? "Select agent"}
              hasAgents={agentList.length > 0}
              host={host}
              onSelectAgent={(next: AvailableAgent) => {
                setPickedAgentId(next.id);
                created.current = null;
              }}
              pendingAgent={null}
              pendingAgentId="__unused_pending_agent__"
              onSelectPending={() => {}}
              onCreateCustomAgent={() => {}}
              allowCreateCustomAgent={false}
              sandboxSelected={false}
              onOpenChange={handleSelectOpenChange}
              dropdownModal={false}
              contentClassName="w-80"
              contentAlign="start"
              triggerClassName="h-8 w-full justify-between rounded-lg border border-input bg-transparent px-2.5 text-foreground hover:bg-transparent hover:text-foreground dark:bg-input/30"
              triggerLabelClassName="max-w-none text-ui"
            />
          </div>

          <div className="flex flex-col gap-1.5">
            <Label htmlFor="design-host">Host</Label>
            {onlineHosts.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                No online hosts. Connect a host to create a design.
              </p>
            ) : (
              <Select
                value={hostId ?? undefined}
                componentId="design.new.host"
                onValueChange={(next) => {
                  setPickedHostId(next);
                  setPickedFolder(null);
                  created.current = null;
                }}
                onOpenChange={handleSelectOpenChange}
              >
                <SelectTrigger
                  id="design-host"
                  data-testid="design-host-trigger"
                  className="w-full"
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent position="popper" align="start">
                  {onlineHosts.map((h) => (
                    <SelectItem key={h.host_id} value={h.host_id}>
                      {h.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
          </div>

          {hostId && (
            <div className="flex flex-col gap-1.5">
              <Label>Folder</Label>
              <Button
                type="button"
                variant="outline"
                className="w-full justify-start gap-2"
                onClick={() => setBrowserOpen(true)}
                componentId="design.new.folder"
              >
                <FolderOpenIcon className="size-4 text-muted-foreground" />
                <span className="truncate">{folder ? "Change folder" : "Choose folder"}</span>
              </Button>
              <WorkspacePickerDialog
                open={browserOpen}
                onOpenChange={setBrowserOpen}
                hostId={hostId}
                initialPath={folder}
                onConfirm={(path) => {
                  setPickedFolder(path);
                  created.current = null;
                }}
              />
              {folder && (
                <p
                  className="truncate font-mono text-sm text-muted-foreground"
                  data-testid="design-folder"
                >
                  {folder}
                </p>
              )}
              {folder && kitHint && (
                <p className="flex items-center gap-1.5 text-sm text-muted-foreground">
                  <PaletteIcon className="size-3.5" aria-hidden />
                  {kitHint}
                </p>
              )}
            </div>
          )}

          {error && (
            <div
              role="alert"
              className="flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive"
            >
              <TriangleAlertIcon className="mt-0.5 size-3.5 shrink-0" />
              <span>{error}</span>
            </div>
          )}
        </div>

        <DialogFooter className="mx-0 mb-0 shrink-0 rounded-none border-t-0 bg-transparent px-6 py-4 sm:justify-end">
          <Button
            variant="outline"
            onClick={() => onOpenChange(false)}
            componentId="design.new.cancel"
          >
            Cancel
          </Button>
          <Button
            onClick={() => void handleCreate()}
            loading={submitting}
            disabled={!canCreate}
            componentId="design.new.create"
          >
            Create
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
