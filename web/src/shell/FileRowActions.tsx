import {
  createContext,
  useContext,
  useRef,
  useState,
  type ReactElement,
  type RefObject,
} from "react";
import { CopyIcon, DownloadIcon, FolderOpenIcon, InfoIcon } from "lucide-react";
import { downloadWorkspaceFile } from "@/hooks/useFileContent";
import type { WorkspaceChangedFile } from "@/hooks/useWorkspaceChangedFiles";
import { copyText } from "@/lib/clipboard";
import { toast } from "sonner";
import { isAbsoluteComposerPath } from "@/lib/composerContext";
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuTrigger,
} from "@/components/ui/context-menu";
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

export const FilesPanelFocusContext = createContext<RefObject<HTMLElement | null> | null>(null);

export function canReceiveFocus(target: HTMLElement | null): target is HTMLElement {
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
  children: (rowRef: RefObject<HTMLDivElement | null>, contextOpen: boolean) => ReactElement;
}

export function FileRowActions({ children, ...props }: FileRowActionsProps) {
  const rowRef = useRef<HTMLDivElement>(null);
  const focusReturnRef = useRef<HTMLElement | null>(null);
  const contextFocusRef = useRef<HTMLElement | null>(null);
  const infoOpenedRef = useRef(false);
  const panelFocusRef = useContext(FilesPanelFocusContext);
  const [contextOpen, setContextOpen] = useState(false);
  const isDeleted = props.isDeleted ?? props.lastKnown ?? false;
  const revealTarget = useRevealTarget(isDeleted ? null : props.revealPath);
  const actionLabel = `More actions for ${props.actionName ?? props.name}`;
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
      void copyText(props.path)
        .then(() => toast.success("Copied to clipboard."))
        .catch(() => toast.error("Copy failed"));
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
      infoOpenedRef.current = true;
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

  const restoreFocusAfterMenuClose = (event: Event) => {
    event.preventDefault();
    if (infoOpenedRef.current) {
      infoOpenedRef.current = false;
      return;
    }
    const target = focusReturnRef.current;
    const fallback = panelFocusRef?.current ?? null;
    if (canReceiveFocus(target)) {
      target.focus();
    } else if (canReceiveFocus(fallback)) {
      fallback.focus();
    }
  };

  return (
    <ContextMenu
      onOpenChange={(open) => {
        setContextOpen(open);
        if (open) {
          const active = document.activeElement;
          const target =
            contextFocusRef.current ??
            (active instanceof HTMLElement &&
            rowRef.current?.contains(active) &&
            canReceiveFocus(active)
              ? active
              : null);
          focusReturnRef.current = target ?? rowRef.current;
          contextFocusRef.current = null;
        }
      }}
    >
      <ContextMenuTrigger
        asChild
        onKeyDown={(event) => {
          if (event.key === "F10" && event.shiftKey) {
            event.preventDefault();
            const rowBounds = event.currentTarget.getBoundingClientRect();
            event.currentTarget.dispatchEvent(
              new MouseEvent("contextmenu", {
                bubbles: true,
                cancelable: true,
                clientX: rowBounds.left,
                clientY: rowBounds.bottom,
              }),
            );
          }
        }}
        onContextMenuCapture={() => {
          const active = document.activeElement;
          contextFocusRef.current =
            active instanceof HTMLElement &&
            rowRef.current?.contains(active) &&
            canReceiveFocus(active)
              ? active
              : null;
        }}
      >
        {children(rowRef, contextOpen)}
      </ContextMenuTrigger>
      <ContextMenuContent
        aria-label={actionLabel}
        onContextMenu={(event) => {
          // Firefox may dispatch its native menu after Shift+F10 opens ours.
          event.preventDefault();
        }}
        onEscapeKeyDown={(event) => event.stopPropagation()}
        onCloseAutoFocus={restoreFocusAfterMenuClose}
      >
        {items.map(({ label, icon: Icon, onSelect }) => (
          <ContextMenuItem key={label} onSelect={onSelect}>
            <Icon className="size-4" />
            {label}
          </ContextMenuItem>
        ))}
      </ContextMenuContent>
    </ContextMenu>
  );
}
