import { Spinner } from "@/components/ui/spinner";

export function PageLoading({ label = "Loading Omnigent…" }: { label?: string }) {
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

/** Loading state for the shell's content area; the surrounding shell stays on screen. */
export function ContentLoading() {
  return (
    <div className="flex min-h-0 flex-1 items-center justify-center">
      <Spinner className="size-5 text-muted-foreground" aria-label="Loading" />
    </div>
  );
}
