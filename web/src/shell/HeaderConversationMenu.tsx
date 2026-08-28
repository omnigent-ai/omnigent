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
  ChevronLeftIcon,
  EllipsisIcon,
  FolderInputIcon,
  GitBranchIcon,
  InfoIcon,
  MailIcon,
  PencilIcon,
  PinIcon,
  PinOffIcon,
  ShareIcon,
  Trash2Icon,
} from "lucide-react";
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
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  PINNED_LABEL_KEY,
  type Conversation,
  useArchiveConversation,
  useMoveToProject,
  useRenameConversation,
  useStopAndDeleteConversation,
  useTogglePinnedConversation,
} from "@/hooks/useConversations";
import { ProjectPicker } from "./ProjectPicker";
import { markConversationUnread } from "@/hooks/useUnseenConversations";
import { useOmnigentAnalytics } from "@/lib/analytics";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { Link, useNavigate } from "@/lib/routing";
import { USER_SESSION_TITLE_MAX_CHARS } from "@/lib/sessionTitles";
import { showToast } from "@/components/ui/toast";
import { cn } from "@/lib/utils";
import { MOBILE_GLASS_SURFACE } from "./mobileGlass";
import { conversationDisplayLabel } from "./sidebarNav";

interface HeaderConversationMenuProps {
  conversation: Conversation;
  currentProject: string | null;
  canShare: boolean;
  shareDisabled?: boolean;
  shareDisabledReason?: string;
  onShare: () => void;
  hasAgentInfo?: boolean;
  onAgentInfo?: () => void;
  /** Mobile workspace-rail entries (Files · Agents · Shells · Logs). */
  workspaceItems?: ReactNode;
}

function ArchivedToast() {
  return (
    <span>
      View archived sessions in{" "}
      <Link to="/settings/archived" className="font-medium text-primary hover:underline">
        Settings
      </Link>
    </span>
  );
}

function showArchivedToast() {
  showToast(<ArchivedToast />);
}

export function HeaderConversationMenu({
  conversation,
  currentProject,
  canShare,
  shareDisabled = false,
  shareDisabledReason,
  onShare,
  hasAgentInfo = false,
  onAgentInfo,
  workspaceItems = null,
}: HeaderConversationMenuProps) {
  const navigate = useNavigate();
  const isMobile = useIsMobileViewport();
  const { trackClick } = useOmnigentAnalytics();
  const togglePinned = useTogglePinnedConversation();
  const rename = useRenameConversation();
  const moveToProject = useMoveToProject();
  const archive = useArchiveConversation();
  const deleteConversation = useStopAndDeleteConversation();
  const [menuOpen, setMenuOpen] = useState(false);
  const [projectPickerOpen, setProjectPickerOpen] = useState(false);
  const [renameOpen, setRenameOpen] = useState(false);
  const [renameTitle, setRenameTitle] = useState(conversation.title ?? "");
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleteBranch, setDeleteBranch] = useState(false);
  const previousConversationId = useRef(conversation.id);
  const isPinned = conversation.labels?.[PINNED_LABEL_KEY] != null;
  const label = conversationDisplayLabel(conversation);
  // Mobile taps need a bigger target than the dense desktop row.
  const itemClass = isMobile ? "gap-2.5 px-2.5 py-2" : undefined;
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
  }, [conversation.id, conversation.title]);

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

  const confirmDelete = () => {
    setDeleteOpen(false);
    setDeleteBranch(false);
    navigate("/", { replace: true });
    deleteConversation.mutate({
      id: conversation.id,
      deleteBranch: gitBranch !== null && deleteBranch,
    });
  };

  const archiveConversation = () => {
    closeMenu();
    archive.mutate(
      { id: conversation.id, archived: true },
      {
        onSuccess: () => {
          navigate("/", { replace: true });
          showArchivedToast();
        },
      },
    );
  };

  const mainItems = (
    <>
      <DropdownMenuItem
        data-testid="header-pin-conversation"
        className={itemClass}
        onSelect={() => togglePinned.mutate({ id: conversation.id, pinned: !isPinned })}
      >
        {isPinned ? <PinOffIcon className="size-3.5" /> : <PinIcon className="size-3.5" />}
        {isPinned ? "Unpin" : "Pin"}
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
      {/* Rename lives here only on mobile — the native shells hide the
          breadcrumb, so this menu is the sole entry point. On desktop the
          shortcut is clicking the breadcrumb title (HeaderTitle). */}
      {isMobile && (
        <DropdownMenuItem
          data-testid="header-rename-conversation"
          className={itemClass}
          onSelect={() => setRenameOpen(true)}
        >
          <PencilIcon className="size-3.5" />
          Rename
        </DropdownMenuItem>
      )}
      <DropdownMenuItem
        data-testid="header-mark-unread-conversation"
        className={itemClass}
        onSelect={() => markConversationUnread(conversation.id, conversation.updated_at)}
      >
        <MailIcon className="size-3.5" />
        Mark as unread
      </DropdownMenuItem>
      {/* Move to project lives here only on mobile — the native mobile shells
          hide the breadcrumb, so this menu is the sole entry point. On desktop
          the shortcut is the breadcrumb's folder tag (HeaderProjectTag). */}
      {isMobile && (
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
      )}
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
        <ArchiveIcon className="size-3.5" />
        Archive
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
        onOpenChange={(open) => {
          setMenuOpen(open);
          if (!open) setProjectPickerOpen(false);
        }}
      >
        <DropdownMenuTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            size={isMobile ? "icon" : "icon-xs"}
            aria-label="Conversation actions"
            data-testid="header-conversation-actions"
            className="shrink-0 border-none text-muted-foreground hover:text-foreground max-md:rounded-full"
          >
            <EllipsisIcon className={isMobile ? "size-4" : "size-3.5"} />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent
          align={isMobile ? "end" : "start"}
          className={cn(
            "min-w-56",
            MOBILE_GLASS_SURFACE,
            isMobile && "max-w-[min(20rem,calc(100vw-1rem))]",
          )}
        >
          {isMobile && !projectPickerOpen && (
            <>
              <DropdownMenuLabel className="truncate px-2.5 pb-1.5 text-foreground">
                {label}
              </DropdownMenuLabel>
              <DropdownMenuSeparator />
            </>
          )}
          {isMobile && projectPickerOpen ? (
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
            <DialogFooter className="mt-4 border-t-0 bg-transparent">
              <Button type="button" variant="ghost" onClick={() => setRenameOpen(false)}>
                Cancel
              </Button>
              <Button type="submit" disabled={!renameTitle.trim() || rename.isPending}>
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
              onClick={confirmDelete}
              disabled={deleteConversation.isPending}
            >
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
