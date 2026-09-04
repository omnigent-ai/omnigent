import { NotFoundPage } from "@/pages/NotFoundPage";
import { useParams } from "@/lib/routing";
import { resolveExtensionPage } from "./catalog";
import { useExtensions } from "./ExtensionProvider";
import { ExtensionPageHost } from "./ExtensionPageHost";

export function ExtensionPageRoute() {
  const params = useParams<{ extensionId: string; "*": string }>();
  const route = params["*"]?.split("/")[0];
  const resolved = resolveExtensionPage(useExtensions(), params.extensionId, route);

  if (!resolved) return <NotFoundPage />;

  return <ExtensionPageHost resolved={resolved} />;
}
