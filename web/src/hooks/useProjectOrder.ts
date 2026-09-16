import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { showToast } from "@/components/ui/toast";
import { getProjectOrder, saveProjectOrder } from "@/lib/projectsApi";
import { resolveOrCreateProjectId, type ProjectSummary } from "./useConversations";

export function useProjectOrder() {
  return useQuery({ queryKey: ["project-order"], queryFn: getProjectOrder });
}

export function useSaveProjectOrder() {
  const client = useQueryClient();
  return useMutation({
    mutationKey: ["project-order"],
    mutationFn: async (projects: ProjectSummary[] | null) => {
      if (projects === null) return saveProjectOrder(null);
      const ids = await Promise.all(
        projects.map((project) => project.id ?? resolveOrCreateProjectId(project.name)),
      );
      return saveProjectOrder(ids);
    },
    onMutate: async (projects) => {
      await client.cancelQueries({ queryKey: ["projects"] });
      const previous = client.getQueryData<ProjectSummary[]>(["projects"]);
      // Reset waits for the server's canonical alphabetical comparison.
      if (projects !== null) client.setQueryData(["projects"], projects);
      return { previous };
    },
    onError: (_error, _projects, context) => {
      if (context?.previous) client.setQueryData(["projects"], context.previous);
      showToast("Couldn't save project order", { duration: 0 });
    },
    onSettled: async () => {
      await Promise.all([
        client.invalidateQueries({ queryKey: ["projects"] }),
        client.invalidateQueries({ queryKey: ["project-order"] }),
      ]);
    },
  });
}
