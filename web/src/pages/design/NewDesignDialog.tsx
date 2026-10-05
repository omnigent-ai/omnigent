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
import { fetchFileContent } from "@/hooks/useFileContent";
import { useHostFilesystem } from "@/hooks/useHostFilesystem";
import { useHosts } from "@/hooks/useHosts";
import { deleteFileContent, writeFileContent } from "@/hooks/useWriteFileContent";
import { isAcpHarnessAgent, selectableSessionAgents } from "@/lib/agentGrouping";
import { planDesignSystemImportFrom, runDesignSystemImport } from "@/lib/designDeckApi";
import { DECK_SUFFIX, deckName } from "@/lib/designDecks";
import type { ImportError, ImportPlan } from "@/lib/designSystemImport";
import {
  DESIGN_SYSTEM_POINTER,
  DS_MANIFEST,
  DS_SKILL,
  NOT_A_DESIGN_SYSTEM,
  designSystemName,
  detectDesignSystemKind,
  folderName,
  readRecentDesignSystems,
  rememberDesignSystem,
  serializeDesignSystemPointer,
  type DesignSystemRef,
} from "@/lib/designSystem";
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
import { ImportProgress, ImportSummary } from "./DesignSystemImport";

function joinPath(folder: string, rel: string): string {
  return `${folder.replace(/\/+$/, "")}/${rel}`;
}

const SYSTEM_NONE = "none";
const SYSTEM_KIT = "kit";
const SYSTEM_CHOOSE = "choose";
const systemValue = (ref: DesignSystemRef) => `ds:${ref.path}`;
const errorText = (e: unknown) => (e instanceof Error && e.message ? e.message : String(e));

/** An import of the chosen system, keyed by its folder. */
type ImportChoice =
  | { path: string; status: "planning" }
  | { path: string; status: "confirm" | "confirmed"; plan: ImportPlan }
  | { path: string; status: "error"; message: string };

/** The manifest or SKILL.md name, read through the new session; else the known name. */
async function resolveSystemName(sessionId: string, ref: DesignSystemRef): Promise<string> {
  const read = async (file: string) => {
    try {
      const f = await fetchFileContent(sessionId, joinPath(ref.path, file));
      return f.encoding === "utf-8" ? f.content : undefined;
    } catch {
      return undefined;
    }
  };
  const [manifest, skill] = await Promise.all([
    ref.kind === "full" ? read(DS_MANIFEST) : undefined,
    read(DS_SKILL),
  ]);
  return manifest || skill ? designSystemName({ folder: ref.path, manifest, skill }) : ref.name;
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
  // null: the default (folder kit, else the host's most recent system, else none).
  const [pickedSystem, setPickedSystem] = useState<string | null>(null);
  const [systemFolder, setSystemFolder] = useState<string | null>(null);
  const [systemBrowserOpen, setSystemBrowserOpen] = useState(false);
  const [importChoice, setImportChoice] = useState<ImportChoice | null>(null);
  const [copy, setCopy] = useState<{ done: number; total: number; errors: ImportError[] } | null>(
    null,
  );
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
      setPickedSystem(null);
      setSystemFolder(null);
      setImportChoice(null);
      setCopy(null);
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

  const recents = useMemo(
    () => (open && hostId ? readRecentDesignSystems(hostId) : []),
    [open, hostId],
  );
  const systemListing = useHostFilesystem(hostId, systemFolder);
  const systemListed =
    systemFolder !== null && !!systemListing.data && !systemListing.isPlaceholderData;
  const chosenKind = systemListed
    ? detectDesignSystemKind(systemListing.data!.entries.map((e) => e.name))
    : null;
  const chosen: DesignSystemRef | null =
    systemFolder && chosenKind
      ? {
          path: systemFolder,
          kind: chosenKind,
          name: recents.find((r) => r.path === systemFolder)?.name ?? folderName(systemFolder),
        }
      : null;
  const systemProblem =
    systemFolder === null
      ? null
      : systemListing.error
        ? systemListing.error.message
        : systemListed && !chosenKind
          ? NOT_A_DESIGN_SYSTEM
          : null;
  const systems =
    chosen && !recents.some((r) => r.path === chosen.path) ? [chosen, ...recents] : recents;
  const kitFound = kitHint === "Kit found";
  const systemOptions = [
    SYSTEM_NONE,
    ...(kitFound ? [SYSTEM_KIT] : []),
    ...systems.map(systemValue),
  ];
  const defaultSystem = kitFound ? SYSTEM_KIT : systems[0] ? systemValue(systems[0]) : SYSTEM_NONE;
  const systemChoice =
    pickedSystem && systemOptions.includes(pickedSystem) ? pickedSystem : defaultSystem;
  const system = systems.find((s) => systemValue(s) === systemChoice);
  const importing = system && importChoice?.path === system.path ? importChoice : null;

  async function planImport(source: DesignSystemRef) {
    if (!hostId) return;
    const path = source.path;
    const settle = (next: ImportChoice) =>
      setImportChoice((current) => (current?.path === path ? next : current));
    setImportChoice({ path, status: "planning" });
    try {
      settle({ path, status: "confirm", plan: await planDesignSystemImportFrom(hostId, path) });
    } catch (e) {
      settle({ path, status: "error", message: errorText(e) });
    }
  }

  const canCreate =
    prompt.trim() !== "" &&
    agent !== undefined &&
    hostId !== null &&
    folder !== "" &&
    !submitting &&
    importing?.status !== "planning" &&
    importing?.status !== "confirm" &&
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
      systemBrowserOpen ||
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
      const named = system && { ...system, name: await resolveSystemName(sessionId, system) };
      let ref = named;
      if (named && importing?.status === "confirmed") {
        const { plan } = importing;
        setCopy({ done: 0, total: plan.files.length, errors: [] });
        const result = await runDesignSystemImport(sessionId, plan, named, (done, total) =>
          setCopy({ done, total, errors: [] }),
        );
        if (!result.ref) {
          setCopy({ done: plan.files.length, total: plan.files.length, errors: result.errors });
          throw new Error(
            `Couldn't import ${result.errors.length} of ${plan.files.length} design-system files.`,
          );
        }
        ref = result.ref;
      } else if (named) {
        await writeFileContent(
          sessionId,
          DESIGN_SYSTEM_POINTER,
          serializeDesignSystemPointer(named),
        );
      } else {
        // A pointer left from an earlier design would override the folder kit.
        await deleteFileContent(sessionId, DESIGN_SYSTEM_POINTER);
      }
      await postEvent(sessionId, {
        type: "message",
        data: {
          role: "user",
          content: [{ type: "input_text", text: firstDesignMessage(prompt, path, ref) }],
        },
      });
      rememberDesignDefaults(agent.id, hostId, folder);
      if (named) rememberDesignSystem(hostId, named);
      void queryClient.invalidateQueries({ queryKey: ["conversations"] });
      onCreated(sessionId, path);
      onOpenChange(false);
    } catch (e) {
      setError(e instanceof Error && e.message ? e.message : "Couldn't create the design.");
    } finally {
      setCopy((c) => (c?.errors.length ? c : null));
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
                  setPickedSystem(null);
                  setSystemFolder(null);
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

          {hostId && (
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="design-system">Design system</Label>
              <Select
                value={systemChoice}
                componentId="design.new.system"
                onValueChange={(next) => {
                  if (next === SYSTEM_CHOOSE) setSystemBrowserOpen(true);
                  else setPickedSystem(next);
                }}
                onOpenChange={handleSelectOpenChange}
              >
                <SelectTrigger
                  id="design-system"
                  data-testid="design-system-trigger"
                  className="w-full"
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent position="popper" align="start">
                  <SelectItem value={SYSTEM_NONE}>None</SelectItem>
                  {kitFound && <SelectItem value={SYSTEM_KIT}>Folder kit</SelectItem>}
                  {systems.map((s) => (
                    <SelectItem key={s.path} value={systemValue(s)}>
                      {`${s.name} (${s.kind})`}
                    </SelectItem>
                  ))}
                  <SelectItem value={SYSTEM_CHOOSE}>Choose folder</SelectItem>
                </SelectContent>
              </Select>
              <WorkspacePickerDialog
                open={systemBrowserOpen}
                onOpenChange={setSystemBrowserOpen}
                hostId={hostId}
                initialPath={system?.path ?? folder}
                onConfirm={(path) => {
                  setSystemFolder(path);
                  setPickedSystem(`ds:${path}`);
                }}
              />
              {system && (
                <div className="flex items-center gap-2">
                  <p
                    className="min-w-0 flex-1 truncate font-mono text-sm text-muted-foreground"
                    data-testid="design-system-path"
                  >
                    {system.path}
                  </p>
                  {(!importing || importing.status === "error") && (
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      disabled={submitting}
                      onClick={() => void planImport(system)}
                      componentId="design.new.system_import"
                    >
                      Import
                    </Button>
                  )}
                </div>
              )}
              {importing?.status === "planning" && (
                <p className="text-sm text-muted-foreground">Listing design-system files...</p>
              )}
              {importing?.status === "error" && (
                <p className="text-sm text-destructive">{`Couldn't import: ${importing.message}`}</p>
              )}
              {(importing?.status === "confirm" || importing?.status === "confirmed") && (
                <div className="flex flex-col gap-2 rounded-md border border-border p-2">
                  <ImportSummary plan={importing.plan} />
                  {importing.status === "confirmed" && (
                    <p className="text-sm text-muted-foreground">
                      The copy is made when you create the design.
                    </p>
                  )}
                  <div className="flex justify-end gap-2">
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      disabled={submitting}
                      onClick={() => setImportChoice(null)}
                      componentId="design.new.system_import_cancel"
                    >
                      {importing.status === "confirmed" ? "Don't import" : "Cancel"}
                    </Button>
                    {importing.status === "confirm" && (
                      <Button
                        type="button"
                        size="sm"
                        disabled={importing.plan.files.length === 0}
                        onClick={() => setImportChoice({ ...importing, status: "confirmed" })}
                        componentId="design.new.system_import_confirm"
                      >
                        Import a copy
                      </Button>
                    )}
                  </div>
                </div>
              )}
              {copy && <ImportProgress done={copy.done} total={copy.total} errors={copy.errors} />}
              {systemProblem && <p className="text-sm text-destructive">{systemProblem}</p>}
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
