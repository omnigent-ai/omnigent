// The floating New Agent panel: the new-chat screen alone, in the desktop shell's
// panel window. Escape and the X hide it, keeping what was typed.
import { useEffect, useMemo, useRef, useState } from "react";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { Toaster } from "@/components/ui/sonner";
import { floatingComposerBridge } from "@/lib/nativeBridge";
import {
  ComposerPresentationContext,
  type ComposerPresentation,
} from "@/shell/composerPresentation";
import { NewChatLandingScreen } from "@/shell/NewChatDialog";
import { FloatingComposerHeader } from "./FloatingComposerHeader";
import { launchFromPanel } from "./floatingLaunch";

export const OPEN_PAGE_ERROR = "Couldn't open that page. Open it from a main window.";

/** Open layers that take Escape themselves; while one is up, Escape doesn't hide the panel. */
const ESCAPE_OWNERS = [
  "[data-radix-popper-content-wrapper]",
  "[role='dialog'][data-state='open']",
  "[role='menu'][data-state='open']",
  "[role='listbox'][data-state='open']",
].join(", ");

function escapeOwnerIsOpen(): boolean {
  return Array.from(document.querySelectorAll(ESCAPE_OWNERS)).some(
    (layer) => layer.closest("[hidden], [aria-hidden='true']") === null,
  );
}

function dismiss() {
  floatingComposerBridge()?.dismiss();
}

export function FloatingComposerPage() {
  // A delivered launch starts a fresh composer; a failed one keeps the prompt where it was.
  const [generation, setGeneration] = useState(0);
  const [error, setError] = useState<string | null>(null);
  // Only the latest launch's outcome counts, so an older one can't clear a newer prompt.
  const latestLaunch = useRef(0);
  const presentation = useMemo<ComposerPresentation>(
    () => ({
      kind: "floating",
      launch: (request) => {
        const launchNumber = ++latestLaunch.current;
        setError(null);
        // Never rejects: every failure comes back as the outcome's error.
        void launchFromPanel(request).then((outcome) => {
          if (launchNumber !== latestLaunch.current) return;
          if (outcome.freshComposer) setGeneration((current) => current + 1);
          setError(outcome.error);
        });
      },
      onFilePicker: (state) => floatingComposerBridge()?.filePicker(state),
      openAppPage: (page) => {
        void Promise.resolve()
          .then(() => floatingComposerBridge()?.openInMainWindow(page))
          .catch(() => null)
          .then((opened) => {
            if (!opened?.ok) setError(OPEN_PAGE_ERROR);
          });
      },
    }),
    [],
  );

  // Escape hides the panel, after any open picker or menu has taken its own Escape.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented || event.isComposing) return;
      if (escapeOwnerIsOpen()) return;
      dismiss();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  return (
    <div className="flex h-dvh flex-col overflow-hidden bg-background text-foreground">
      <FloatingComposerHeader onClose={dismiss} />
      {error === null ? null : (
        <Alert
          variant="destructive"
          className="mx-3 mt-2 w-auto"
          data-testid="floating-launch-error"
        >
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}
      <main className="flex min-h-0 flex-1 flex-col">
        <ComposerPresentationContext.Provider value={presentation}>
          <NewChatLandingScreen key={generation} />
        </ComposerPresentationContext.Provider>
      </main>
      {/* The app shell's toaster isn't mounted here. */}
      <Toaster position="top-center" />
    </div>
  );
}
