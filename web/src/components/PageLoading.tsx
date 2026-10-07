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
