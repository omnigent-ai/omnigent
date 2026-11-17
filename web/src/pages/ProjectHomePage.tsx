import { useEffect, type KeyboardEvent } from "react";
import { PageScroll } from "@/components/PageScroll";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { useParams } from "@/lib/routing";
import {
  useProjectBoardSearchStore,
  useProjectComposerStore,
} from "@/projectHome/ProjectHomeHeader";
import { ProjectKanbanBoard } from "@/projectHome/ProjectKanbanBoard";
import { NewChatLandingScreen } from "@/shell/NewChatDialog";

/** A project's home: its sessions board, with the header's composer floating over it. */
export function ProjectHomePage() {
  const { projectName = "" } = useParams<{ projectName: string }>();
  const isMobile = useIsMobileViewport();
  const composerOpen = useProjectComposerStore((state) => state.open) && !isMobile;
  const setComposerOpen = useProjectComposerStore((state) => state.setOpen);
  useEffect(() => () => setComposerOpen(false), [setComposerOpen]);
  const searchQuery = useProjectBoardSearchStore((state) => state.query);
  const setSearchQuery = useProjectBoardSearchStore((state) => state.setQuery);
  // Each project starts unfiltered.
  useEffect(() => () => setSearchQuery(""), [projectName, setSearchQuery]);

  function closeOnEscape(event: KeyboardEvent<HTMLDivElement>) {
    // Portaled menus bubble through React too; they handle their own Escape.
    if (
      event.key === "Escape" &&
      !event.defaultPrevented &&
      event.currentTarget.contains(event.target as Node)
    ) {
      setComposerOpen(false);
    }
  }

  return (
    <div className="relative flex min-h-0 flex-1 flex-col">
      {/* Only the lanes scroll; the page stays put. */}
      <PageScroll
        className="overflow-hidden"
        maxWidthClassName="max-w-none"
        contentClassName="flex h-full flex-col"
        // Clears the taller header the project title gets.
        extraTop="4rem"
        extraBottom="0px"
      >
        <ProjectKanbanBoard
          projectName={projectName}
          searchQuery={searchQuery}
          overlayClearance={composerOpen ? "10rem" : undefined}
        />
      </PageScroll>
      {composerOpen && (
        <div className="pointer-events-none absolute inset-x-0 bottom-0 z-20 flex justify-center px-4 pb-6">
          <div
            data-testid="project-floating-composer"
            className="pointer-events-auto w-full max-w-3xl [filter:drop-shadow(var(--shadow-dialog))]"
            onKeyDown={closeOnEscape}
          >
            <NewChatLandingScreen
              projectName={projectName}
              onSessionStarted={() => setComposerOpen(false)}
            />
          </div>
        </div>
      )}
    </div>
  );
}
