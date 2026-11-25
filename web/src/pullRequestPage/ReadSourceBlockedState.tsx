// Shown in place of a PR chosen for this Mac when this Mac can't read it. The read failed
// before asking anyone, so this appears at once, with the one way to read it now.

import { LaptopIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { chooseReadSource } from "@/lib/desktopReadPath";

function pullRequestHost(url: string): string | null {
  try {
    return new URL(url).hostname.toLowerCase();
  } catch {
    return null;
  }
}

export function ReadSourceBlockedState({ prUrl }: { prUrl: string }) {
  const host = pullRequestHost(prUrl);
  return (
    <div
      role="status"
      className="flex h-full flex-col items-center justify-center gap-3 p-6 text-center"
    >
      <LaptopIcon aria-hidden className="size-8 text-muted-foreground/50" />
      <p className="text-ui font-medium text-foreground">This Mac can’t read this pull request.</p>
      {host && (
        <p className="max-w-xs text-ui text-muted-foreground">
          This Mac doesn’t read pull requests from {host}.
        </p>
      )}
      <Button size="sm" variant="outline" onClick={() => chooseReadSource(prUrl, "session")}>
        Use the session’s machine
      </Button>
    </div>
  );
}
