import { useHosts } from "@/hooks/useHosts";
import { inferHostSshTarget } from "@/lib/editorLinks";

/** Standard Databricks SSH alias inferred from the existing host-list row. */
export function useHostSshTarget(hostId: string | null): string | null {
  const { data: hosts } = useHosts({ includeSandbox: true, enabled: hostId !== null });
  return inferHostSshTarget(hosts?.find((host) => host.host_id === hostId));
}
