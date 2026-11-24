// The floating New Agent panel's header: what the panel is for, and Close.
import { SparklesIcon, XIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { useLogoUrl } from "@/lib/branding";

export function FloatingComposerHeader({ onClose }: { onClose: () => void }) {
  // An operator's own logo when the deployment has one; otherwise a neutral mark.
  const logoUrl = useLogoUrl("main");
  return (
    <header className="floating-composer-header flex shrink-0 items-center gap-3 border-b border-border px-4 py-2">
      <span
        className="flex size-7 shrink-0 items-center justify-center rounded-lg bg-muted text-brand-accent"
        aria-hidden
      >
        {logoUrl ? (
          <img src={logoUrl} alt="" className="size-4 object-contain" />
        ) : (
          <SparklesIcon className="size-4" />
        )}
      </span>
      <div className="flex min-w-0 flex-1 flex-col">
        <h1 className="truncate text-sm font-semibold">New Agent</h1>
        <p className="truncate text-xs text-muted-foreground">
          Describe a task, attach files, and launch an agent
        </p>
      </div>
      <Button
        variant="ghost"
        size="icon-sm"
        className="shrink-0 text-muted-foreground hover:text-foreground"
        aria-label="Close New Agent"
        title="Close (Esc)"
        onClick={onClose}
      >
        <XIcon />
      </Button>
    </header>
  );
}
