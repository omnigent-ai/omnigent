import { PageScroll } from "@/components/PageScroll";
import { useParams } from "@/lib/routing";
import { ProjectKanbanBoard } from "@/projectHome/ProjectKanbanBoard";
import { NewChatLandingScreen } from "@/shell/NewChatDialog";

/** A project's home: its new-session composer above its sessions board. */
export function ProjectHomePage() {
  const { projectName = "" } = useParams<{ projectName: string }>();
  return (
    <PageScroll maxWidthClassName="max-w-none" contentClassName="flex min-h-full flex-col">
      <NewChatLandingScreen projectName={projectName} />
      <hr className="mx-4 border-border md:mx-6" />
      <div className="flex flex-1 flex-col pt-4">
        <ProjectKanbanBoard projectName={projectName} />
      </div>
    </PageScroll>
  );
}
