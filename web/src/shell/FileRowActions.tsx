import {
  createContext,
  useContext,
  useRef,
  useState,
  type ReactElement,
  type ReactNode,
  type RefObject,
} from "react";
import { CopyIcon, DownloadIcon, FolderOpenIcon, InfoIcon, MoreHorizontalIcon } from "lucide-react";
import { downloadWorkspaceFile } from "@/hooks/useFileContent";
import type { WorkspaceChangedFile } from "@/hooks/useWorkspaceChangedFiles";
import { copyText } from "@/lib/clipboard";
import { toast } from "sonner";
import { cn } from "@/lib/utils";
import { useIsCoarsePointer } from "@/hooks/useIsCoarsePointer";
import { isAbsoluteComposerPath } from "@/lib/composerContext";
import { Button } from "@/components/ui/button";
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuTrigger,
} from "@/components/ui/context-menu";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { revealInFileManager, revealLabel, useRevealTarget } from "./RevealInFileManager";

export interface FileRowInfo {
  name: string;
  path: string;
  kind: "file" | "folder";
  bytes?: number | null;
  modifiedAt?: number | null;
  status?: WorkspaceChangedFile["status"];
  linesAdded?: number | null;
  linesRemoved?: number | null;
  lastKnown?: boolean;
}

interface FileRowActionItem {
  label: string;
  icon: typeof DownloadIcon;
  onSelect: () => void;
}

export const ROW_MENU_SLOT_CLASS =
  "transition-[width] group-hover:w-20 group-focus-within:w-20 group-data-[state=open]:w-20 group-data-[actions-open=true]:w-20 pointer-coarse:w-20";
export const ROW_MENU_SIZE_SLOT_CLASS =
  "transition-[width] group-hover:w-20 group-focus-within:w-20 group-data-[state=open]:w-20 group-data-[actions-open=true]:w-20 pointer-coarse:w-[76px]";

export const FilesPanelFocusContext = createContext<RefObject<HTMLElement | null> | null>(null);

function canReceiveFocus(target: HTMLElement | null): target is HTMLElement {
  return Boolean(
    target?.isConnected &&
    !target.matches(":disabled") &&
    !target.closest("[inert], [aria-hidden='true']"),
  );
}

interface FileRowActionsProps extends FileRowInfo {
  revealPath: string | null;
  conversationId?: string;
  downloadable?: boolean;
  isDeleted?: boolean;
  onBrowse?: () => void;
  actionName?: string;
  onOpenInfo: (info: FileRowInfo, returnFocus: HTMLElement | null) => void;
  children: (
    moreActions: ReactNode,
    rowRef: RefObject<HTMLDivElement | null>,
    primaryActionRef: RefObject<HTMLButtonElement | null>,
    actionsOpen: boolean,
  ) => ReactElement;
}

export function FileRowActions({ children, ...props }: FileRowActionsProps) {
  const rowRef = useRef<HTMLDivElement>(null);
  const primaryActionRef = useRef<HTMLButtonElement>(null);
  const kebabRef = useRef<HTMLButtonElement>(null);
  const focusReturnRef = useRef<HTMLElement | null>(null);
  const contextFocusRef = useRef<HTMLElement | null>(null);
  const panelFocusRef = useContext(FilesPanelFocusContext);
  const [contextOpen, setContextOpen] = useState(false);
  const [dropdownOpen, setDropdownOpen] = useState(false);
  const isCoarsePointer = useIsCoarsePointer();
  const isDeleted = props.isDeleted ?? props.lastKnown ?? false;
  const revealTarget = useRevealTarget(isDeleted ? null : props.revealPath);
  const items: FileRowActionItem[] = [];

  if (props.kind === "file" && !isDeleted && props.downloadable && props.conversationId) {
    items.push({
      label: "Download",
      icon: DownloadIcon,
      onSelect: () => {
        void downloadWorkspaceFile(props.conversationId!, props.path).catch(() =>
          toast.error("Download failed"),
        );
      },
    });
  }
  items.push({
    label: `Copy ${isAbsoluteComposerPath(props.path) ? "absolute" : "relative"} path`,
    icon: CopyIcon,
    onSelect: () => {
      void copyText(props.path).catch(() => toast.error("Copy failed"));
    },
  });
  if (revealTarget) {
    items.push({
      label: revealLabel(props.kind === "folder"),
      icon: FolderOpenIcon,
      onSelect: () => revealInFileManager(revealTarget),
    });
  }
  if (props.kind === "folder" && !isDeleted && props.onBrowse) {
    items.unshift({
      label: "Browse folder",
      icon: FolderOpenIcon,
      onSelect: props.onBrowse,
    });
  }
  items.push({
    label: `${props.kind === "file" ? "File" : "Folder"} info${isDeleted ? " (last known)" : ""}`,
    icon: InfoIcon,
    onSelect: () => {
      const { name, path, kind, bytes, modifiedAt, status, linesAdded, linesRemoved } = props;
      props.onOpenInfo(
        {
          name,
          path,
          kind,
          bytes,
          modifiedAt,
          status,
          linesAdded,
          linesRemoved,
          lastKnown: isDeleted,
        },
        focusReturnRef.current,
      );
    },
  });

  const renderItems = (Item: typeof ContextMenuItem | typeof DropdownMenuItem) =>
    items.map(({ label, icon: Icon, onSelect }) => (
      <Item key={label} onSelect={onSelect}>
        <Icon className="size-4" />
        {label}
      </Item>
    ));

  const kebab = (
    <DropdownMenu
      onOpenChange={(open) => {
        setDropdownOpen(open);
        if (open) focusReturnRef.current = kebabRef.current;
      }}
    >
      <DropdownMenuTrigger asChild>
        <Button
          ref={kebabRef}
          type="button"
          variant="ghost"
          size="icon-sm"
          aria-label={`More actions for ${props.actionName ?? props.name}`}
          onClick={(event) => event.stopPropagation()}
          className={cn(
            "size-[18px] shrink-0 rounded p-0.5 text-muted-foreground transition-opacity hover:bg-muted hover:text-foreground",
            isCoarsePointer || contextOpen
              ? "opacity-100"
              : "opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 focus-visible:opacity-100 data-[state=open]:opacity-100",
          )}
        >
          <MoreHorizontalIcon className="size-3.5" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" onEscapeKeyDown={(event) => event.stopPropagation()}>
        {renderItems(DropdownMenuItem)}
      </DropdownMenuContent>
    </DropdownMenu>
  );

  return (
    <ContextMenu
      onOpenChange={(open) => {
        setContextOpen(open);
        if (open) {
          const active = document.activeElement;
          const target =
            contextFocusRef.current ??
            (active instanceof HTMLElement &&
            active !== rowRef.current &&
            rowRef.current?.contains(active) &&
            canReceiveFocus(active)
              ? active
              : null);
          focusReturnRef.current = target
            ? target
            : canReceiveFocus(primaryActionRef.current)
              ? primaryActionRef.current
              : kebabRef.current;
          contextFocusRef.current = null;
        }
      }}
    >
      <ContextMenuTrigger
        asChild
        onContextMenuCapture={() => {
          const active = document.activeElement;
          contextFocusRef.current =
            active instanceof HTMLElement &&
            active !== rowRef.current &&
            rowRef.current?.contains(active) &&
            canReceiveFocus(active)
              ? active
              : null;
        }}
      >
        {children(kebab, rowRef, primaryActionRef, contextOpen || dropdownOpen)}
      </ContextMenuTrigger>
      <ContextMenuContent
        onEscapeKeyDown={(event) => event.stopPropagation()}
        onCloseAutoFocus={(event) => {
          event.preventDefault();
          const target = focusReturnRef.current;
          const fallback = panelFocusRef?.current ?? null;
          if (canReceiveFocus(target)) {
            target.focus();
          } else if (canReceiveFocus(fallback)) {
            fallback.focus();
          }
        }}
      >
        {renderItems(ContextMenuItem)}
      </ContextMenuContent>
    </ContextMenu>
  );
}
