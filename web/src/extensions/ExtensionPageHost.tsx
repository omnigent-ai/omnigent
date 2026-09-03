import type { ResolvedExtensionPage } from "./types";
import { useRefreshExtensions } from "./ExtensionProvider";
import { ExtensionViewHost } from "./ExtensionViewHost";

export function ExtensionPageHost({ resolved }: { resolved: ResolvedExtensionPage }) {
  const refresh = useRefreshExtensions();
  return (
    <ExtensionViewHost extension={resolved.extension} page={resolved.page} refresh={refresh} />
  );
}
