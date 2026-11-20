import { Spinner } from "@/components/ui/spinner";

/**
 * The page's loading state. `quiet` paints only the background, for a window that should
 * look already there while it boots, such as the desktop's New Agent panel.
 */
export function PageLoading({
  label = "Loading Omnigent…",
  quiet = false,
}: {
  label?: string;
  quiet?: boolean;
}) {
  if (quiet) {
    return (
      <div className="min-h-screen bg-background" role="status" aria-busy="true">
        <span className="sr-only">{label}</span>
      </div>
    );
  }
  return (
    <div
      className="flex min-h-screen items-center justify-center gap-3 bg-background text-muted-foreground"
      role="status"
      aria-live="polite"
      aria-busy="true"
    >
      <Spinner className="size-5 motion-reduce:animate-none" aria-hidden="true" />
      <span>{label}</span>
    </div>
  );
}
