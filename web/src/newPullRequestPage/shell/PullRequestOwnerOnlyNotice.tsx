import { Button } from "@/components/ui/button";

/** Says why a shared session's pull request offers no changes here, and where to make them. */
export function PullRequestOwnerOnlyNotice({ prUrl }: { prUrl: string }) {
  return (
    <div
      role="status"
      data-pull-request-owner-only=""
      className="flex flex-wrap items-center gap-x-2 gap-y-1 border-b border-border px-4 py-2 text-ui"
    >
      <p className="min-w-0 text-muted-foreground">
        Only the session’s owner can change this pull request here.
      </p>
      <Button asChild variant="link" size="sm" className="h-auto p-0">
        <a href={prUrl} target="_blank" rel="noreferrer">
          Open on GitHub
        </a>
      </Button>
    </div>
  );
}
