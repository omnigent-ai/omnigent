import { useQuery } from "@tanstack/react-query";
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

/** Resolve standard aliases before the click, using metadata only for user-connected hosts. */
export function useHostSshTarget(
  hostId: string | null,
  conversationId: string | undefined,
): string | null {
  const { data: hosts } = useHosts({ includeSandbox: true, enabled: hostId !== null });
  const host = hosts?.find((candidate) => candidate.host_id === hostId);
  const online = host?.status === "online";
  const providerTarget = inferHostSshTarget(host);
  const discover =
    online && host.sandbox_provider == null && !!conversationId && !isTempConvId(conversationId);
  const { data: discoveredTarget, isError } = useQuery({
    queryKey: ["hostSshTarget", conversationId, hostId],
    enabled: discover,
    staleTime: 5 * 60_000,
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
  if (!online) return null;
  return providerTarget ?? (discover && !isError ? (discoveredTarget ?? null) : null);
}
