import {
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import {
  ArchiveIcon,
  ArchiveRestoreIcon,
  ChevronLeftIcon,
  ClockPlusIcon,
  DownloadIcon,
  EllipsisIcon,
  FolderInputIcon,
  GitBranchIcon,
  GitForkIcon,
  InfoIcon,
  MailIcon,
  PencilIcon,
  PinIcon,
  PinOffIcon,
  PlusIcon,
  ShareIcon,
  Trash2Icon,
} from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import {
  ARIA_MOD_KEY,
  ARCHIVE_SESSION_KEYS,
  FORK_SESSION_KEYS,
  CompactShortcutKeys,
  PIN_SESSION_KEYS,
} from "@/components/KeyboardShortcut";
import { SessionActionMenuItem } from "@/components/SessionActionMenuItem";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useIsMutating, useQueryClient } from "@tanstack/react-query";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isIndentedSibling } from "@/lib/sessionFamilies";
import { PIN_WRITE_MUTATION_KEY } from "@/lib/sessionListCache";
import { exportSessionTranscript } from "@/lib/sessionsApi";
import { triggerBrowserDownload } from "@/hooks/useFileContent";
import {
  PINNED_LABEL_KEY,
  type Conversation,
  useArchiveConversation,
  useMoveToProject,
  useRenameConversation,
  useStopAndDeleteConversation,
  useTogglePinnedConversation,
  useAutomationsEnabled,
} from "@/hooks/useConversations";
import { CreateAutomationFromSessionDialog } from "@/components/scheduled/CreateAutomationFromSessionDialog";
import { AutomationSourceDeleteNotice } from "@/components/scheduled/AutomationSourceDeleteNotice";
import {
  canCreateAutomationFromSession,
  automationSourceTaskId,
  useDeleteSourceAutomations,
} from "@/lib/automationFromSession";
import { ProjectPicker } from "./ProjectPicker";
import { markConversationUnread } from "@/hooks/useUnseenConversations";
import { useOmnigentAnalytics } from "@/lib/analytics";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { useDesktopCommand } from "@/lib/desktopCommands";
import { supportsDesktopCommands } from "@/lib/nativeBridge";
import { useIsEmbedded } from "@/lib/embedded";
import { getEmbedRoot } from "@/lib/host";
import { hasCommandModifier, isFocusInDialogOrMenu } from "@/lib/hotkeys";
import { useNavigate } from "@/lib/routing";
import { USER_SESSION_TITLE_MAX_CHARS } from "@/lib/sessionTitles";
import { showArchiveUndoToast } from "./archiveUndoToast";
import { unpinWithUndo } from "./unpinUndoToast";
import { useArchiveWorktreePrompt } from "./ArchiveWorktreeDialog";
import { cn } from "@/lib/utils";
import { MOBILE_GLASS_SURFACE } from "./mobileGlass";
import { conversationDisplayLabel } from "./sidebarNav";
import { MobileHeaderButton } from "./MobileHeaderButton";
import { SessionMenuHeader } from "./SessionMenuHeader";

interface HeaderConversationMenuProps {
  conversation: Conversation;
  workingDirectory?: string | null;
  currentProject: string | null;
  canShare: boolean;
  canFork: boolean;
  forkDisabledReason?: string;
  shareDisabled?: boolean;
  shareDisabledReason?: string;
  onShare: () => void;
  onFork: () => void;
  hasAgentInfo?: boolean;
  onAgentInfo?: () => void;
  /**
   * Mobile Chat/Terminal view switch (ViewModeMenuItems) — leads the menu on
   * terminal-first sessions and carries its own trailing separator. `null`
   * otherwise.
   */
  viewItems?: ReactNode;
  /** Mobile workspace-rail entries (Files · Agents · Shells · Logs). */
  workspaceItems?: ReactNode;
}

export function HeaderConversationMenu({
  conversation,
  workingDirectory = conversation.workspace,
  currentProject,
  canShare,
  canFork,
  forkDisabledReason,
  shareDisabled = false,
  shareDisabledReason,
  onShare,
  onFork,
  hasAgentInfo = false,
  onAgentInfo,
  viewItems = null,
  workspaceItems = null,
}: HeaderConversationMenuProps) {
  const navigate = useNavigate();
  const info = useServerInfo();
  const queryClient = useQueryClient();
  const isMobile = useIsMobileViewport();
  const isEmbedded = useIsEmbedded();
  const { trackClick } = useOmnigentAnalytics();
  const togglePinned = useTogglePinnedConversation();
  // Pin writes don't overlap, so Pin/Unpin is disabled while one is saving.
  const pinSaving = useIsMutating({ mutationKey: PIN_WRITE_MUTATION_KEY }) > 0;
  const rename = useRenameConversation();
  const moveToProject = useMoveToProject();
  const archive = useArchiveConversation();
  const archiveWorktreePrompt = useArchiveWorktreePrompt();
  const deleteConversation = useStopAndDeleteConversation();
  const [menuOpen, setMenuOpen] = useState(false);
  const [projectPickerOpen, setProjectPickerOpen] = useState(false);
  const [renameOpen, setRenameOpen] = useState(false);
  const [renameTitle, setRenameTitle] = useState(conversation.title ?? "");
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleteBranch, setDeleteBranch] = useState(false);
  const [automationOpen, setAutomationOpen] = useState(false);
  const automationsEnabled = useAutomationsEnabled();
  // This menu is only offered to the session's owner (see ChatHeader).
  const canCreateAutomation = canCreateAutomationFromSession({
    info,
    automationsEnabled,
    conversation,
    isOwner: true,
  });
  const automationSourceOf = automationSourceTaskId(conversation);
  const deleteSourceAutomations = useDeleteSourceAutomations();
  const previousConversationId = useRef(conversation.id);
  const isPinned = conversation.labels?.[PINNED_LABEL_KEY] != null;
  const isArchived = conversation.archived === true;
  const canAddAgent = !isArchived && info !== "loading" && info.sibling_sessions;
  const addAgentPath = `/?sibling=${encodeURIComponent(conversation.id)}`;
  const label = conversationDisplayLabel(conversation);
  // Mobile taps need a bigger target than the dense desktop row.
  const itemClass = cn("mobile-touch-target", isMobile && "gap-2.5 px-2.5 py-2");
  // Share and Agent info reach this menu on mobile from the header's legacy
  // Share · Agent info menu, which reported them under these ids. Keep
  // emitting the same ones so the metric series stays continuous, and only on
  // mobile — the desktop kebab is a different surface.
  const trackMobile = (componentId: string) => {
    if (isMobile) trackClick(componentId, "button");
  };
  const gitBranch = conversation.git_branch ?? null;

  useEffect(() => {
    if (!renameOpen) setRenameTitle(conversation.title ?? "");
  }, [conversation.title, renameOpen]);

  useEffect(() => {
    if (previousConversationId.current === conversation.id) return;
    previousConversationId.current = conversation.id;
    setMenuOpen(false);
    setProjectPickerOpen(false);
    setRenameOpen(false);
    setRenameTitle(conversation.title ?? "");
    setDeleteOpen(false);
    setDeleteBranch(false);
    setAutomationOpen(false);
  }, [conversation.id, conversation.title]);

  useEffect(() => {
    if (!canAddAgent) return;
    const handleAddAgent = (event: globalThis.KeyboardEvent) => {
      if (
        event.defaultPrevented ||
        event.repeat ||
        event.isComposing ||
        !hasCommandModifier(event) ||
        !event.altKey ||
        event.shiftKey ||
        event.getModifierState("AltGraph") ||
        event.code !== "KeyA"
      ) {
        return;
      }
      const embedRoot = getEmbedRoot();
      if (isEmbedded && (!embedRoot || !event.composedPath().includes(embedRoot))) return;
      if (isFocusInDialogOrMenu() || document.activeElement?.closest(".xterm, .monaco-editor"))
        return;
      event.preventDefault();
      event.stopPropagation();
      navigate(addAgentPath);
    };
    window.addEventListener("keydown", handleAddAgent);
    return () => window.removeEventListener("keydown", handleAddAgent);
  }, [addAgentPath, canAddAgent, isEmbedded, navigate]);

  const closeMenu = () => {
    setMenuOpen(false);
    setProjectPickerOpen(false);
  };

  const handleProjectSelect = (project: string) => {
    closeMenu();
    moveToProject.mutate({ id: conversation.id, project });
  };

  const submitRename = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const title = renameTitle.trim();
    if (title && title !== (conversation.title ?? "")) {
      rename.mutate({ id: conversation.id, title });
    }
    setRenameOpen(false);
  };

  const confirmDelete = async () => {
    setDeleteOpen(false);
    setDeleteBranch(false);
    // "Delete both": the automation first, so no run starts from a missing session.
    if (automationSourceOf !== null && !(await deleteSourceAutomations([automationSourceOf]))) {
      toast.error("Couldn't delete the automation, so the session was kept");
      return;
    }
    navigate("/", { replace: true });
    deleteConversation.mutate({
      id: conversation.id,
      deleteBranch: gitBranch !== null && deleteBranch,
    });
  };

  const exportConversation = async () => {
    try {
      const jsonl = await exportSessionTranscript(conversation.id);
      triggerBrowserDownload(
        new Blob([jsonl], { type: "application/jsonl" }),
        `${conversation.id}.jsonl`,
      );
    } catch {
      toast.error("Export failed");
    }
  };

  const archiveConversation = () => {
    closeMenu();
    if (isArchived) {
      // Unarchiving keeps the user on the session — no redirect home and no
      // Undo toast (mirrors the sidebar row's Unarchive).
      archive.mutate({ id: conversation.id, archived: false });
      return;
    }
    archiveWorktreePrompt.requestArchive([conversation], (deleteWorktreeIds) =>
      archiveNow(deleteWorktreeIds.has(conversation.id)),
    );
  };

  const archiveNow = (deleteWorktree: boolean) => {
    // The row leaves the sidebar optimistically (useArchiveConversation flips
    // the cached `archived` flag in onMutate), and we're viewing the session
    // being archived, so leave its chat surface now — synchronously, like
    // confirmDelete — rather than in an onSuccess callback that fires a
    // round-trip later with a stale active session.
    navigate("/", { replace: true });
    archive.mutate({ id: conversation.id, archived: true, deleteWorktree });
    // Fire NOW, not in a mutate onSuccess: navigating away unmounts this menu,
    // and per-call mutate callbacks don't fire once their observer unmounts.
    // The Undo toast is driven by module state + the app-level Toaster, so it
    // survives this menu unmounting.
    showArchiveUndoToast(queryClient, [conversation], navigate);
  };

  const togglePin = () =>
    isPinned
      ? unpinWithUndo(queryClient, togglePinned.mutateAsync, conversation.id, conversation)
      : togglePinned.mutate({ id: conversation.id, pinned: true });
  // Desktop Session → Pin (⌥⌘P), Archive (⇧⌘A) and Fork (⌥⌘F) act on the session shown here.
  // Like the menu item, Pin waits out a pending pin write (from any row).
  useDesktopCommand("toggle-pin-session", pinSaving ? null : togglePin, { active: isPinned });
  useDesktopCommand("archive-session", archiveConversation, { active: isArchived });
  useDesktopCommand("fork-session", canFork && !forkDisabledReason ? onFork : null);
  const desktopShortcuts = supportsDesktopCommands();

  const mainItems = (
    <>
      {canAddAgent && (
        <>
          <DropdownMenuItem
            data-testid="add-sibling-session"
            className={itemClass}
            aria-keyshortcuts={`${ARIA_MOD_KEY}+Alt+A`}
            onSelect={() => navigate(addAgentPath)}
          >
            <PlusIcon className="size-3.5" />
            Add nested agent
          </DropdownMenuItem>
          <DropdownMenuSeparator />
        </>
      )}
      {/* Chat/Terminal renders its own trailing separator (null on other sessions). */}
      {viewItems}
      <DropdownMenuItem
        data-testid="header-pin-conversation"
        className={itemClass}
        disabled={pinSaving}
        onSelect={togglePin}
      >
        {isPinned ? <PinOffIcon className="size-3.5" /> : <PinIcon className="size-3.5" />}
        {isPinned ? "Unpin" : "Pin"}
        {desktopShortcuts && (
          <CompactShortcutKeys keys={PIN_SESSION_KEYS} className="ml-auto pl-4" />
        )}
      </DropdownMenuItem>
      {canShare && (
        <DropdownMenuItem
          data-testid="header-share-conversation"
          className={itemClass}
          disabled={shareDisabled}
          title={shareDisabledReason}
          onSelect={
            shareDisabled
              ? undefined
              : () => {
                  trackMobile("chat.header.mobile_share");
                  onShare();
                }
          }
        >
          <ShareIcon className="size-3.5" />
          Share
        </DropdownMenuItem>
      )}
      {canFork && (
        <SessionActionMenuItem
          data-testid="header-fork-conversation"
          disabledReason={forkDisabledReason}
          className={itemClass}
          onSelect={onFork}
        >
          <GitForkIcon className="size-3.5" />
          Fork
          {desktopShortcuts && (
            <CompactShortcutKeys keys={FORK_SESSION_KEYS} className="ml-auto pl-4" />
          )}
        </SessionActionMenuItem>
      )}
      <DropdownMenuItem
        data-testid="header-export-conversation"
        className={itemClass}
        onSelect={() => void exportConversation()}
      >
        <DownloadIcon className="size-3.5" />
        Export
      </DropdownMenuItem>
      {hasAgentInfo && onAgentInfo && (
        <DropdownMenuItem
          data-testid="header-agent-info"
          className={itemClass}
          onSelect={() => {
            trackMobile("chat.header.mobile_agent_info");
            onAgentInfo();
          }}
        >
          <InfoIcon className="size-3.5" />
          Agent info
        </DropdownMenuItem>
      )}
      {/* Rename is also reachable on desktop by clicking the breadcrumb title
          (HeaderTitle); on mobile the native shells hide the breadcrumb, so this
          menu is the sole entry point. */}
      <DropdownMenuItem
        data-testid="header-rename-conversation"
        className={itemClass}
        onSelect={() => setRenameOpen(true)}
      >
        <PencilIcon className="size-3.5" />
        Rename
      </DropdownMenuItem>
      <DropdownMenuItem
        data-testid="header-mark-unread-conversation"
        className={itemClass}
        onSelect={() => markConversationUnread(conversation.id, conversation.updated_at)}
      >
        <MailIcon className="size-3.5" />
        Mark as unread
      </DropdownMenuItem>
      {canCreateAutomation && (
        <DropdownMenuItem
          data-testid="header-create-automation"
          className={itemClass}
          onSelect={() => setAutomationOpen(true)}
        >
          <ClockPlusIcon className="size-3.5" />
          Create automation
        </DropdownMenuItem>
      )}
      {/* Move to project is also reachable on desktop via the breadcrumb's
          folder tag (HeaderProjectTag); on mobile the native shells hide the
          breadcrumb, so this menu is the sole entry point. */}
      {!isIndentedSibling(conversation) &&
        (isMobile ? (
          // Mobile has no room for a side flyout, so this item swaps the menu body
          // to the project picker in place (see the `projectPickerOpen` branch).
          <DropdownMenuItem
            data-testid="header-move-to-project"
            className={cn("whitespace-nowrap", itemClass)}
            onSelect={(event) => {
              event.preventDefault();
              setProjectPickerOpen(true);
            }}
          >
            <FolderInputIcon className="size-3.5" />
            {currentProject ? "Move session" : "Add to project"}
          </DropdownMenuItem>
        ) : (
          <DropdownMenuSub>
            <DropdownMenuSubTrigger
              data-testid="header-move-to-project"
              className="whitespace-nowrap"
            >
              <FolderInputIcon className="size-3.5" />
              {currentProject ? "Move session" : "Add to project"}
            </DropdownMenuSubTrigger>
            {/* A native submenu flyout — no separate popover layer, so no
              open/dismiss race with the parent menu. */}
            <DropdownMenuSubContent className="min-w-56">
              <ProjectPicker currentProject={currentProject} onSelect={handleProjectSelect} />
            </DropdownMenuSubContent>
          </DropdownMenuSub>
        ))}
      {workspaceItems && (
        <>
          <DropdownMenuSeparator />
          {workspaceItems}
        </>
      )}
      <DropdownMenuSeparator />
      <DropdownMenuItem
        data-testid="header-archive-conversation"
        className={itemClass}
        onSelect={archiveConversation}
      >
        {isArchived ? (
          <ArchiveRestoreIcon className="size-3.5" />
        ) : (
          <ArchiveIcon className="size-3.5" />
        )}
        {isArchived ? "Unarchive" : "Archive"}
        {desktopShortcuts && (
          <CompactShortcutKeys keys={ARCHIVE_SESSION_KEYS} className="ml-auto pl-4" />
        )}
      </DropdownMenuItem>
      <DropdownMenuItem
        data-testid="header-delete-conversation"
        className={itemClass}
        variant="destructive"
        onSelect={() => setDeleteOpen(true)}
      >
        <Trash2Icon className="size-3.5" />
        Delete
      </DropdownMenuItem>
    </>
  );

  return (
    <>
      <DropdownMenu
        open={menuOpen}
        // Radix's modal mode sets `pointer-events: none` on <body> while the
        // menu is open, leaving the menu as the only touch target on screen.
        // Browser touch-target adjustment then snaps outside taps near the
        // menu onto it, so on a phone the menu can't be dismissed. Non-modal
        // keeps the page interactive, so an outside tap lands on real content
        // and dismisses the menu.
        modal={!isMobile}
        onOpenChange={(open) => {
          setMenuOpen(open);
          if (!open) setProjectPickerOpen(false);
        }}
      >
        <DropdownMenuTrigger asChild>
          <MobileHeaderButton
            type="button"
            variant="ghost"
            size={isMobile ? "icon" : "icon-xs"}
            aria-label="Conversation actions"
            data-testid="header-conversation-actions"
            className="mobile-touch-target shrink-0 border-none text-muted-foreground hover:text-foreground max-md:size-11"
          >
            <EllipsisIcon className={isMobile ? "size-5" : "size-3.5"} />
          </MobileHeaderButton>
        </DropdownMenuTrigger>
        <DropdownMenuContent
          align={isMobile ? "end" : "start"}
          collisionPadding={isMobile ? 16 : undefined}
          className={cn(
            "min-w-56",
            MOBILE_GLASS_SURFACE,
            isMobile && "w-[calc(100vw-2rem)] max-w-[calc(100vw-2rem)]",
          )}
        >
          {isMobile && !projectPickerOpen && (
            <SessionMenuHeader title={label} workingDirectory={workingDirectory} />
          )}
          {projectPickerOpen ? (
            <>
              <DropdownMenuItem
                data-testid="header-project-picker-back"
                className={itemClass}
                onSelect={(event) => {
                  event.preventDefault();
                  setProjectPickerOpen(false);
                }}
              >
                <ChevronLeftIcon className="size-3.5" />
                Back
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              <ProjectPicker currentProject={currentProject} onSelect={handleProjectSelect} />
            </>
          ) : (
            mainItems
          )}
        </DropdownMenuContent>
      </DropdownMenu>

      {archiveWorktreePrompt.dialog}
      {automationOpen && (
        <CreateAutomationFromSessionDialog
          conversation={conversation}
          open
          onOpenChange={setAutomationOpen}
        />
      )}
      <Dialog open={renameOpen} onOpenChange={setRenameOpen}>
        <DialogContent>
          <form onSubmit={submitRename}>
            <DialogHeader>
              <DialogTitle>Rename session</DialogTitle>
              <DialogDescription>Choose a short name that is easy to find later.</DialogDescription>
            </DialogHeader>
            <input
              autoFocus
              aria-label="Session name"
              data-testid="header-rename-conversation-input"
              maxLength={USER_SESSION_TITLE_MAX_CHARS}
              value={renameTitle}
              onChange={(event) => setRenameTitle(event.target.value)}
              onKeyDown={(event: KeyboardEvent<HTMLInputElement>) => {
                if (event.key === "Escape") setRenameOpen(false);
              }}
              className="mt-4 h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm outline-none focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/50"
            />
            <DialogFooter className="mt-4 border-t-0 bg-transparent mobile-app:flex-col-reverse mobile-app:gap-1">
              <Button
                type="button"
                variant="ghost"
                className="mobile-touch-target mobile-app:w-full"
                onClick={() => setRenameOpen(false)}
              >
                Cancel
              </Button>
              <Button
                type="submit"
                className="mobile-touch-target mobile-app:w-full"
                disabled={!renameTitle.trim() || rename.isPending}
              >
                Rename
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <Dialog
        open={deleteOpen}
        onOpenChange={(open) => {
          setDeleteOpen(open);
          if (!open) setDeleteBranch(false);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete conversation?</DialogTitle>
            <DialogDescription>
              <span className="font-medium break-all">{label}</span> and all of its history will be
              removed. This cannot be undone.
            </DialogDescription>
          </DialogHeader>
          {automationSourceOf !== null && (
            <AutomationSourceDeleteNotice taskId={automationSourceOf} />
          )}
          {gitBranch !== null && (
            <div className="flex flex-col gap-2 rounded-md border border-destructive/40 bg-destructive/5 p-3">
              <p className="text-sm text-muted-foreground">
                Optionally clean up the git worktree. These actions are{" "}
                <span className="font-semibold text-destructive">irreversible</span>.
              </p>
              <label className="flex cursor-pointer items-start gap-2 text-ui">
                <input
                  type="checkbox"
                  data-testid="header-delete-branch-checkbox"
                  checked={deleteBranch}
                  onChange={(event) => setDeleteBranch(event.target.checked)}
                  className="mt-0.5 size-4 shrink-0 accent-destructive"
                />
                <GitBranchIcon className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" />
                <span className="min-w-0">
                  Delete local branch{" "}
                  <code className="break-all rounded bg-muted px-1 py-0.5 text-sm">
                    {gitBranch}
                  </code>
                </span>
              </label>
            </div>
          )}
          <DialogFooter className="border-t-0 bg-transparent">
            <Button
              type="button"
              variant="ghost"
              onClick={() => setDeleteOpen(false)}
              disabled={deleteConversation.isPending}
            >
              Cancel
            </Button>
            <Button
              type="button"
              variant="destructive"
              onClick={() => void confirmDelete()}
              disabled={deleteConversation.isPending}
            >
              {automationSourceOf !== null ? "Delete both" : "Delete"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
