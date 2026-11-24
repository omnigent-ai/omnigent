import { useCallback, useMemo } from "react";
import { queryOptions, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchFileContent } from "@/hooks/useFileContent";
import { useHosts } from "@/hooks/useHosts";
import { inferHostSshTarget, parseArcaTypeLabel } from "@/lib/editorLinks";
import { isTempConvId } from "@/lib/tempConversationId";

async function readMetadataText(conversationId: string, path: string): Promise<string | null> {
  const file = await fetchFileContent(conversationId, path);
  return file.object === "session.environment.filesystem.file_content" &&
    file.encoding === "utf-8" &&
    !file.truncated &&
    typeof file.content === "string"
    ? file.content
    : null;
}

/** A user-connected host's SSH name, read from its metadata files through one of its sessions. */
function hostSshTargetQuery(hostId: string | null, conversationId: string | undefined) {
  return queryOptions({
    // Every session on a host gives the same answer, so one read serves them all.
    queryKey: ["hostSshTarget", hostId],
    staleTime: Infinity,
    gcTime: Infinity,
    retry: false,
    queryFn: async () => {
      const labels = await readMetadataText(conversationId!, "/etc/metric_proxy/labels/default");
      const type = labels === null ? null : parseArcaTypeLabel(labels);
      if (type === "arca") return "arca.ssh";
      if (type !== "arclet") return null;
      const name = await readMetadataText(conversationId!, "/opt/arclet/arclet-name");
      return inferHostSshTarget({ name: name?.trim() ?? "", sandbox_provider: "arclet" });
    },
  });
}

/** Whether `hostId` is online, its provider's alias, and whether its metadata names it instead. */
function useSshTargetSource(hostId: string | null, conversationId: string | undefined) {
  const { data: hosts } = useHosts({ includeSandbox: true, enabled: hostId !== null });
  const host = hosts?.find((candidate) => candidate.host_id === hostId);
  const online = host?.status === "online";
  const discover =
    online && host.sandbox_provider == null && !!conversationId && !isTempConvId(conversationId);
  return { online, providerTarget: inferHostSshTarget(host), discover };
}

/**
 * `hostId`'s SSH name for editor links. A provider's host is named by its alias; a
 * user-connected host's name is read from its metadata only once something asks, by passing
 * `resolve` or through {@link useResolveHostSshTarget}, and is null until then.
 */
export function useHostSshTarget(
  hostId: string | null,
  conversationId: string | undefined,
  { resolve = false }: { resolve?: boolean } = {},
): string | null {
  const { online, providerTarget, discover } = useSshTargetSource(hostId, conversationId);
  const { data: discoveredTarget, isError } = useQuery({
    ...hostSshTargetQuery(hostId, conversationId),
    enabled: discover && resolve,
  });
  if (!online) return null;
  return providerTarget ?? (discover && !isError ? (discoveredTarget ?? null) : null);
}

/**
 * Read `hostId`'s SSH name unless it's known, for a menu that offers opening files over SSH to
 * call as it opens. Every {@link useHostSshTarget} for the host shows the name once it's read.
 */
export function useResolveHostSshTarget(
  hostId: string | null,
  conversationId: string | undefined,
): () => void {
  const queryClient = useQueryClient();
  const { discover } = useSshTargetSource(hostId, conversationId);
  const query = useMemo(
    () => (discover ? hostSshTargetQuery(hostId, conversationId) : null),
    [discover, hostId, conversationId],
  );
  return useCallback(() => {
    if (query) void queryClient.prefetchQuery(query);
  }, [queryClient, query]);
}
