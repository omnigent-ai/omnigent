import { useState } from "react";
import { CheckIcon, FolderIcon, PlusIcon, SearchIcon } from "lucide-react";
import { DropdownMenuItem } from "@/components/ui/dropdown-menu";
import { useProjects } from "@/hooks/useConversations";

/**
 * Leading visual for a project row: the project's emoji icon when set,
 * otherwise a folder glyph. Decorative only — row names carry the semantics.
 */
export function ProjectRowIcon({ icon }: { icon?: string | null }) {
  return icon ? (
    <span
      aria-hidden="true"
      className="shrink-0 text-[14px] leading-none"
      data-testid="project-icon"
    >
      {icon}
    </span>
  ) : (
    <FolderIcon aria-hidden="true" className="size-3.5 shrink-0 text-muted-foreground" />
  );
}

/**
 * Searchable list of projects to file a session into, rendered inside a
 * dropdown/submenu. Selecting a project calls `onSelect(name)`; the "Remove
 * from …" row (shown only when already filed) calls `onSelect("")` to unfile.
 * A typed name with no exact match offers a "+ Create <name>" row — the move
 * itself creates the project on demand (see `moveConversationToProject`).
 *
 * Shared by the desktop title folder-tag shortcut (HeaderProjectTag) and the
 * mobile session menu's in-place project view (HeaderConversationMenu).
 */
export function ProjectPicker({
  currentProject,
  onSelect,
}: {
  currentProject: string | null;
  onSelect: (project: string) => void;
}) {
  const { data: projects = [] } = useProjects();
  const [search, setSearch] = useState("");
  const trimmed = search.trim();
  const filtered = trimmed
    ? projects.filter((project) => project.name.toLowerCase().includes(trimmed.toLowerCase()))
    : projects;
  // Offer create only when the typed name isn't already an exact project.
  const canCreate =
    trimmed.length > 0 &&
    !projects.some((project) => project.name.toLowerCase() === trimmed.toLowerCase());
  const currentProjectIcon = projects.find((project) => project.name === currentProject)?.icon;

  return (
    <>
      <div className="flex items-center gap-2 border-b px-2 py-1.5">
        <SearchIcon className="size-3.5 shrink-0 text-muted-foreground" />
        <input
          aria-label="Search or create project"
          className="w-full bg-transparent text-sm outline-none placeholder:text-muted-foreground"
          placeholder="Search or create project"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          onKeyDown={(event) => event.stopPropagation()}
        />
      </div>
      <div className="max-h-48 overflow-y-auto">
        {filtered.map((project) => (
          <DropdownMenuItem
            key={project.name}
            className="px-2 py-1"
            textValue={project.name}
            onSelect={() => onSelect(project.name)}
          >
            <ProjectRowIcon icon={project.icon} />
            <span className="flex-1 truncate text-left">{project.name}</span>
            {currentProject === project.name && (
              <CheckIcon className="size-3.5 shrink-0 text-primary" />
            )}
          </DropdownMenuItem>
        ))}
        {filtered.length === 0 && !canCreate && (
          <p className="px-2 py-1.5 text-sm text-muted-foreground">No projects yet.</p>
        )}
      </div>
      {canCreate && (
        <div className="border-t pt-1">
          <DropdownMenuItem className="px-2 py-1" onSelect={() => onSelect(trimmed)}>
            <PlusIcon className="size-3.5 shrink-0 text-muted-foreground" />
            Create{" "}
            <span className="truncate rounded bg-muted px-1 py-0.5 font-mono text-[0.95em]">
              {trimmed}
            </span>
          </DropdownMenuItem>
        </div>
      )}
      {currentProject && (
        <div className="border-t pt-1">
          <DropdownMenuItem
            className="px-2 py-1"
            textValue={`Remove from ${currentProject}`}
            onSelect={() => onSelect("")}
          >
            <ProjectRowIcon icon={currentProjectIcon} />
            Remove from{" "}
            <span className="rounded bg-muted px-1 py-0.5 font-mono text-[0.95em]">
              {currentProject}
            </span>
          </DropdownMenuItem>
        </div>
      )}
    </>
  );
}
