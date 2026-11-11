// How a typed name matches projects, shared by every project picker so they offer the same rows.

interface NamedProject {
  name: string;
}

/** Projects whose name contains `query`, ignoring case and outer spaces; all of them for an empty query. */
export function projectsMatching<Project extends NamedProject>(
  projects: readonly Project[],
  query: string,
): readonly Project[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return projects;
  return projects.filter((project) => project.name.toLowerCase().includes(needle));
}

/** Whether `query` names a new project: not blank, and no project has that name in any case. */
export function canCreateProject(projects: readonly NamedProject[], query: string): boolean {
  const name = query.trim().toLowerCase();
  if (!name) return false;
  return !projects.some((project) => project.name.toLowerCase() === name);
}
