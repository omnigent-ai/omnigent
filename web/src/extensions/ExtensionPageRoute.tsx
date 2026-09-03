import { NotFoundPage } from "@/pages/NotFoundPage";
import { useParams } from "@/lib/routing";
import { resolveExtensionPage } from "./catalog";
import { useExtensions } from "./ExtensionProvider";

export function ExtensionPageRoute() {
  const params = useParams<{ extensionId: string; "*": string }>();
  const route = params["*"]?.split("/")[0];
  const resolved = resolveExtensionPage(useExtensions(), params.extensionId, route);

  if (!resolved) return <NotFoundPage />;

  return (
    <div className="flex h-full min-h-0 flex-1 items-center justify-center p-6">
      <section className="max-w-md rounded-lg border bg-card p-6 text-center shadow-sm">
        <h1 className="text-lg font-semibold">{resolved.page.title}</h1>
        <p className="mt-2 text-sm text-muted-foreground">
          The extension runtime will be available in the next framework layer.
        </p>
        <p className="mt-3 font-mono text-xs text-muted-foreground">
          {resolved.extension.id} · {resolved.page.view}
        </p>
      </section>
    </div>
  );
}
