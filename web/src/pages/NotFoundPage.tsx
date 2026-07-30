import { Link } from "@/lib/routing";
import { Button } from "@/components/ui/button";

/**
 * Generic 404 page for unmatched client routes. Reached via React
 * Router's wildcard route inside the AppShell layout, so the sidebar
 * remains visible. The server's SPA fallback (`_SPAStaticFiles`) hands
 * any extensionless URL to the SPA, so a typed-in `/foo` lands here
 * after the bundle boots — not on a server-rendered 404.
 */
export function NotFoundPage() {
  return (
    <div className="flex flex-1 items-center justify-center px-6">
      <div className="flex max-w-sm flex-col items-center gap-3 text-center">
        <h1 className="font-medium text-foreground text-lg">Page not found</h1>
        <p className="text-muted-foreground text-sm">
          The URL you followed doesn't match any route in this app.
        </p>
        <Button asChild variant="outline">
          <Link to="/">Back to home</Link>
        </Button>
      </div>
    </div>
  );
}
