import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { showToast } from "@/components/ui/toast";
import { getProjectOrder, saveProjectOrder, type ProjectOrder } from "@/lib/projectsApi";
import { resolveOrCreateProjectId, type ProjectSummary } from "./useConversations";

export function useProjectOrder() {
  return useQuery({ queryKey: ["project-order"], queryFn: getProjectOrder });
}

function orderProjects(projects: ProjectSummary[], order: ProjectSummary[] | null) {
  if (order === null)
    return [...projects].sort((left, right) => {
      if (left.name === right.name) return 0;
      return left.name < right.name ? -1 : 1;
    });
  const ids = new Map(order.map((project, index) => [project.id, index]));
  ids.delete(null);
  const names = new Map(order.map((project, index) => [project.name, index]));
  const rank = (project: ProjectSummary) =>
    ids.get(project.id) ?? names.get(project.previousName ?? project.name) ?? order.length;
  return [...projects].sort((left, right) => rank(left) - rank(right));
}

const confirmedOrders = new WeakMap<
  QueryClient,
  { projects: ProjectSummary[] | null; preference: ProjectOrder | undefined }
>();

export function useSaveProjectOrder() {
  const client = useQueryClient();
  return useMutation({
    mutationKey: ["project-order"],
    scope: { id: "project-order" },
    mutationFn: async (projects: ProjectSummary[] | null) => {
      if (projects === null) return saveProjectOrder(null);
      const current = client.getQueryData<ProjectSummary[]>(["projects"]);
      const remaining = projects.flatMap((project) => {
        const latest = current
          ? current.find((item) =>
              project.id !== null
                ? item.id === project.id
                : item.name === project.name || item.previousName === project.name,
            )
          : project;
        return latest && latest.pending !== "Deleting" && !(latest.id === null && latest.pending)
          ? [latest]
          : [];
      });
      const ids = await Promise.all(
        remaining.map((project) => project.id ?? resolveOrCreateProjectId(project.name)),
      );
      return saveProjectOrder([...new Set(ids)]);
    },
    onMutate: async (projects) => {
      await Promise.all([
        client.cancelQueries({ queryKey: ["projects"] }),
        client.cancelQueries({ queryKey: ["project-order"] }),
      ]);
      const current = client.getQueryData<ProjectSummary[]>(["projects"]);
      if (!confirmedOrders.has(client)) {
        const preference = client.getQueryData<ProjectOrder>(["project-order"]);
        confirmedOrders.set(client, {
          projects:
            preference?.sort_mode === "alphabetical"
              ? null
              : (current ??
                preference?.ordered_project_ids?.map((id) => ({ id, name: "" })) ??
                null),
          preference,
        });
      }
      if (current) client.setQueryData(["projects"], orderProjects(current, projects));
      client.setQueryData<ProjectOrder>(["project-order"], (previous) => ({
        sort_mode: projects ? "manual" : "alphabetical",
        ordered_project_ids: projects
          ? projects.flatMap((project) => (project.id ? [project.id] : []))
          : (previous?.ordered_project_ids ?? null),
      }));
    },
    onSuccess: (preference, projects) => {
      confirmedOrders.set(client, {
        projects,
        preference,
      });
      if (client.isMutating({ mutationKey: ["project-order"] }) === 1)
        client.setQueryData(["project-order"], preference);
    },
    onError: () => {
      if (client.isMutating({ mutationKey: ["project-order"] }) !== 1) return;
      const confirmed = confirmedOrders.get(client);
      if (confirmed) {
        client.setQueryData<ProjectSummary[]>(["projects"], (current) =>
          current ? orderProjects(current, confirmed.projects) : current,
        );
        if (confirmed.preference) client.setQueryData(["project-order"], confirmed.preference);
      }
      showToast("Couldn't save project order", { duration: 0 });
    },
    onSettled: () => {
      if (client.isMutating({ mutationKey: ["project-order"] }) > 1) return;
      confirmedOrders.delete(client);
      void client.invalidateQueries({ queryKey: ["projects"] });
      void client.invalidateQueries({ queryKey: ["project-order"] });
    },
  });
}
