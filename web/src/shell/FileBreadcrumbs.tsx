// The viewed file's path as breadcrumbs: each segment lists its siblings and, on
// right-click, offers its file actions, including rooting the Files tree at a folder.

import { Fragment, useLayoutEffect, useRef, useState } from "react";
import { XIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTitle, PopoverTrigger } from "@/components/ui/popover";
import { useResolvedThemeMode } from "@/components/theme/useResolvedThemeMode";
import { joinBrowseLocation } from "@/hooks/useWorkspaceChangedFiles";
import { useWorkspacePaths } from "@/shell/FileViewerContext";
import { cn } from "@/lib/utils";
import { CopyPathButton } from "./CopyPathButton";
import { useFileCommandMenu } from "./fileCommands/useFileCommandMenu";
import { useRememberedBrowseLocation } from "./filesPane/browseLocations";
import { planFolderAsTreeRoot, showTreeRoot } from "./fileTree/treeRoot";
import {
  type BreadcrumbSegment,
  breadcrumbSegments,
  canBrowseBreadcrumbs,
  folderTitle,
  pathWithinFolder,
  segmentFolder,
} from "./breadcrumbPaths";
import { TabStripScroller, scrollTabIntoView } from "./TabStripScroller";
import { WorkspaceFolderTree } from "./WorkspaceFolderTree";

interface FileBreadcrumbsProps {
  conversationId: string;
  /** The viewed file, workspace-relative or absolute. */
  path: string;
  /** Titles the workspace root's tree, e.g. the workspace folder's name. */
  workspaceName: string;
  /** Opens another file; without it the breadcrumbs only show the path. */
  onOpenFile?: (path: string) => void;
  /** Whether this viewer may root the Files tree outside the workspace. */
  canBrowseOutside?: boolean;
}

const SEGMENT_CLASS =
  "shrink-0 whitespace-nowrap rounded-[var(--radius-sm)] px-1 py-0.5 text-sm text-muted-foreground outline-none hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring data-[state=open]:bg-muted data-[state=open]:text-foreground";

export function FileBreadcrumbs({
  conversationId,
  path,
  workspaceName,
  onOpenFile,
  canBrowseOutside = false,
}: FileBreadcrumbsProps) {
  const segments = breadcrumbSegments(path);
  const browsable = onOpenFile !== undefined && canBrowseBreadcrumbs(path);
  // The segment whose tree is open; another file or conversation closes it for good.
  const viewedFile = `${conversationId}\0${path}`;
  const [openSegment, setOpenSegment] = useState<string | null>(null);
  const [treeFile, setTreeFile] = useState(viewedFile);
  if (treeFile !== viewedFile) {
    setTreeFile(viewedFile);
    setOpenSegment(null);
  }
  // A new file's name replaces the old one without resizing the strip, so show it here.
  const navRef = useRef<HTMLElement>(null);
  useLayoutEffect(() => {
    scrollTabIntoView(navRef.current?.querySelector<HTMLElement>('[aria-current="page"]') ?? null);
  }, [viewedFile]);
  const dark = useResolvedThemeMode() === "dark";
  const { root: workspaceRoot } = useWorkspacePaths();
  const browsed = useRememberedBrowseLocation(conversationId);
  /** Rooting the Files tree at a folder segment, when the tree can show it there. */
  const rootAt = (segment: BreadcrumbSegment) => {
    if (segment.isFile) return undefined;
    const plan = planFolderAsTreeRoot({
      folder: segment.path,
      browsed,
      workspaceRoot,
      canBrowseOutside,
    });
    return plan.availability.available ? () => showTreeRoot(conversationId, plan.root) : undefined;
  };
  const tree = (segment: BreadcrumbSegment) => {
    const folder = segmentFolder(segment);
    const title = folderTitle(folder, workspaceName);
    return (
      <FolderTreeContent
        title={title}
        onClose={() => setOpenSegment(null)}
        tree={
          <WorkspaceFolderTree
            conversationId={conversationId}
            folder={folder}
            revealPath={pathWithinFolder(segment.path, folder)}
            label={`Files in ${title}`}
            dark={dark}
            onOpenFile={(pathInFolder) => {
              setOpenSegment(null);
              onOpenFile?.(joinBrowseLocation(folder, pathInFolder));
            }}
          />
        }
      />
    );
  };

  const renderSegment = (segment: BreadcrumbSegment) => {
    const className = cn(SEGMENT_CLASS, segment.isFile && "text-foreground");
    if (!browsable) {
      return (
        <span className={className} aria-current={segment.isFile ? "page" : undefined}>
          {segment.label}
        </span>
      );
    }
    return (
      <SegmentWithMenu segment={segment} onOpenAsRoot={rootAt(segment)}>
        {(menuProps) => (
          <Popover
            open={openSegment === segment.path}
            onOpenChange={(open) => setOpenSegment(open ? segment.path : null)}
          >
            <PopoverTrigger asChild>
              <button
                type="button"
                className={className}
                aria-current={segment.isFile ? "page" : undefined}
                title={`Show items beside ${segment.label}`}
                {...menuProps}
              >
                {segment.label}
              </button>
            </PopoverTrigger>
            {openSegment === segment.path && tree(segment)}
          </Popover>
        )}
      </SegmentWithMenu>
    );
  };

  return (
    <nav ref={navRef} aria-label="File path" className="flex min-w-0 items-center gap-1">
      {/* The whole path scrolls sideways; the file's own segment stays in view. */}
      <TabStripScroller fadeEdges>
        <ol className="flex items-center">
          {segments.map((segment, index) => (
            <Fragment key={segment.path}>
              {index > 0 && (
                <li aria-hidden className="shrink-0 px-0.5 text-sm text-muted-foreground/70">
                  ›
                </li>
              )}
              <li className="flex shrink-0 items-center">{renderSegment(segment)}</li>
            </Fragment>
          ))}
        </ol>
      </TabStripScroller>
      <span className="shrink-0">
        <CopyPathButton path={path} command="copy-relative-path" />
      </span>
    </nav>
  );
}

/** A segment's right-click menu: its file actions, a folder's with rooting the tree there. */
function SegmentWithMenu({
  segment,
  onOpenAsRoot,
  children,
}: {
  segment: BreadcrumbSegment;
  onOpenAsRoot: (() => void) | undefined;
  children: (menuProps: ReturnType<typeof useFileCommandMenu>["rowProps"]) => React.ReactNode;
}) {
  const { rowProps, menu } = useFileCommandMenu(segment.path, {
    directory: !segment.isFile,
    opens: onOpenAsRoot ? { onOpenAsRoot } : undefined,
  });
  return (
    <>
      {children(rowProps)}
      {menu}
    </>
  );
}

function FolderTreeContent({
  title,
  onClose,
  tree,
}: {
  title: string;
  onClose: () => void;
  tree: React.ReactNode;
}) {
  return (
    <PopoverContent
      align="start"
      tabIndex={-1}
      className="h-80 w-80 gap-0 overflow-hidden p-0"
      aria-label={`Browse ${title}`}
      // Focus stays in the popover while the tree loads, so Tab reaches Close
      // or Retry; a loaded tree then moves it to the viewed file.
      onOpenAutoFocus={(event) => {
        event.preventDefault();
        (event.currentTarget as HTMLElement).focus();
      }}
    >
      <div className="flex h-9 shrink-0 items-center gap-2 border-b border-border pr-1 pl-3">
        <PopoverTitle className="min-w-0 flex-1 truncate" title={title}>
          {title}
        </PopoverTitle>
        <Button type="button" variant="ghost" size="icon-xs" aria-label="Close" onClick={onClose}>
          <XIcon />
        </Button>
      </div>
      {tree}
    </PopoverContent>
  );
}
