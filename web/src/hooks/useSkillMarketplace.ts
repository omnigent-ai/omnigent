import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  addSkillSource,
  deleteInstalledSkill,
  deleteSkillSource,
  fetchInstalledSkillContent,
  fetchSourceCatalog,
  installSkills,
  listInstalledSkills,
  listSkillSources,
  previewCatalog,
  setInstalledSkillEnabled,
  updateInstalledSkill,
  type SkillScope,
} from "@/lib/skillsApi";

const SOURCES_KEY = ["skill-marketplace", "sources"] as const;
const INSTALLED_KEY = ["skill-marketplace", "installed"] as const;
const catalogKey = (sourceId: string) => ["skill-marketplace", "catalog", sourceId] as const;

export function useSkillSources(enabled: boolean) {
  return useQuery({
    queryKey: SOURCES_KEY,
    queryFn: ({ signal }) => listSkillSources(signal),
    enabled,
  });
}

export function useInstalledSkills(enabled: boolean) {
  return useQuery({
    queryKey: INSTALLED_KEY,
    queryFn: ({ signal }) => listInstalledSkills(signal),
    enabled,
  });
}

/** A configured source's catalog; cloning takes a moment, so no refetch on focus. */
export function useSourceCatalog(sourceId: string | undefined, enabled: boolean) {
  return useQuery({
    queryKey: catalogKey(sourceId ?? ""),
    queryFn: ({ signal }) => fetchSourceCatalog(sourceId!, false, signal),
    enabled: enabled && sourceId !== undefined,
    staleTime: 5 * 60 * 1000,
    refetchOnWindowFocus: false,
    retry: false,
  });
}

export function useInstalledSkillContent(skillId: string | undefined) {
  return useQuery({
    queryKey: ["skill-marketplace", "content", skillId],
    queryFn: ({ signal }) => fetchInstalledSkillContent(skillId!, signal),
    enabled: skillId !== undefined,
    retry: false,
  });
}

function useInvalidate() {
  const client = useQueryClient();
  return () => {
    void client.invalidateQueries({ queryKey: ["skill-marketplace"] });
  };
}

export function useAddSkillSource() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: (input: { source: string; ref?: string | null; name?: string | null }) =>
      addSkillSource(input),
    onSuccess: invalidate,
  });
}

export function useDeleteSkillSource() {
  const invalidate = useInvalidate();
  return useMutation({ mutationFn: (id: string) => deleteSkillSource(id), onSuccess: invalidate });
}

export function usePreviewCatalog() {
  return useMutation({
    mutationFn: (input: { source: string; ref?: string | null }) => previewCatalog(input),
  });
}

export function useInstallSkills() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: (input: {
      source: string;
      ref?: string | null;
      names: string[];
      scope?: SkillScope;
    }) => installSkills(input),
    onSuccess: invalidate,
  });
}

export function useUpdateInstalledSkill() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: (id: string) => updateInstalledSkill(id),
    onSuccess: invalidate,
  });
}

export function useSetInstalledSkillEnabled() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
      setInstalledSkillEnabled(id, enabled),
    onSuccess: invalidate,
  });
}

export function useDeleteInstalledSkill() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: (id: string) => deleteInstalledSkill(id),
    onSuccess: invalidate,
  });
}
