import { useEffect, useRef, useState, type ReactNode } from "react";
import {
  ArchiveIcon,
  ChevronLeftIcon,
  EllipsisIcon,
  FolderInputIcon,
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
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  PINNED_LABEL_KEY,
  type Conversation,
  useMoveToProject,
  useTogglePinnedConversation,
} from "@/hooks/useConversations";
import { ProjectPicker } from "./ProjectPicker";
import { markConversationUnread } from "@/hooks/useUnseenConversations";
import { useOmnigentAnalytics } from "@/lib/analytics";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { cn } from "@/lib/utils";
import { MOBILE_GLASS_SURFACE } from "./mobileGlass";
import { conversationDisplayLabel } from "./sidebarNav";
import {
  DeleteSessionDialog,
  RenameSessionDialog,
  useArchiveSessionAction,
} from "./SessionActionDialogs";

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
  const isMobile = useIsMobileViewport();
  const { trackClick } = useOmnigentAnalytics();
  const togglePinned = useTogglePinnedConversation();
  const moveToProject = useMoveToProject();
  const archiveSession = useArchiveSessionAction();
  const [menuOpen, setMenuOpen] = useState(false);
  const [projectPickerOpen, setProjectPickerOpen] = useState(false);
  const [renameOpen, setRenameOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
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

  useEffect(() => {
    if (previousConversationId.current === conversation.id) return;
    previousConversationId.current = conversation.id;
    setMenuOpen(false);
    setProjectPickerOpen(false);
    setRenameOpen(false);
    setDeleteOpen(false);
  }, [conversation.id]);

  const closeMenu = () => {
    setMenuOpen(false);
    setProjectPickerOpen(false);
  };

  const handleProjectSelect = (project: string) => {
    closeMenu();
    moveToProject.mutate({ id: conversation.id, project });
  };

  const archiveConversation = () => {
    closeMenu();
    archiveSession(conversation, true);
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
      {/* Move to project is also reachable on desktop via the breadcrumb's
          folder tag (HeaderProjectTag); on mobile the native shells hide the
          breadcrumb, so this menu is the sole entry point. */}
      {isMobile ? (
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

      <RenameSessionDialog
        conversation={conversation}
        open={renameOpen}
        onOpenChange={setRenameOpen}
      />
      <DeleteSessionDialog
        conversation={conversation}
        open={deleteOpen}
        onOpenChange={setDeleteOpen}
      />
    </>
  );
}
