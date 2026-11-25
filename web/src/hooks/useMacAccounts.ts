import { useQuery } from "@tanstack/react-query";

import { useDesktopReadPathVersion } from "@/lib/desktopReadPath";
import { desktopMacAccounts, type MacAccountChoice } from "@/lib/nativeBridge";

/**
 * `host`'s GitHub accounts on this Mac and which one reads its PRs; null in
 * browsers, older shells, or while `host` is null. Re-read whenever the read
 * path's store changes, e.g. after a sign-in, sign-out, or choice.
 */
export function useMacAccountChoice(
  host: string | null,
  /** Also re-read when this moves, e.g. a login the page reads as by name signed out. */
  alsoAfter = 0,
): MacAccountChoice | null {
  const version = useDesktopReadPathVersion();
  const { data } = useQuery({
    queryKey: ["mac-code-host-accounts", host, version, alsoAfter],
    queryFn: () => desktopMacAccounts(host!),
    enabled: !!host,
    staleTime: 30_000,
    placeholderData: (previous) => previous,
  });
  return data ?? null;
}
