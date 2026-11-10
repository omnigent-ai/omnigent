import { lazy, Suspense } from "react";
import { ArcaShutdownToast } from "@/components/ArcaShutdownToast";
import { KeyboardShortcutsDialog } from "@/components/KeyboardShortcutsDialog";
import { ImportReviewGate } from "@/components/onboarding/HostImportReview";
import { Toaster } from "@/components/ui/sonner";
import type { Conversation } from "@/hooks/useConversations";
import { isFeatureEnabled, type ServerInfo } from "@/lib/capabilities";
import { isElectronShell } from "@/lib/nativeBridge";
import { updateOverlayToastOffset } from "@/lib/updateOverlayInset";
import { CommandPalette } from "./CommandPalette";
import { RecentSessionsSwitcher } from "./RecentSessionsSwitcher";

// Dev-only preview; lazy so the sample data never ships in production bundles.
const ImportContextPreview = import.meta.env.DEV
  ? lazy(() => import("@/components/onboarding/ImportContextPreview"))
  : null;

export interface AppShellOverlaysProps {
  isEmbedded: boolean;
  serverInfo: ServerInfo | "loading";
  sessionSearch: boolean;
  commandPaletteOpen: boolean;
  setCommandPaletteOpen: (open: boolean) => void;
  toggleLeftSidebar: () => void;
  toggleRightPanel: () => void;
  gridRoute: boolean;
  allConversations: Conversation[] | undefined;
  serverConversationId: string | undefined;
  updateOverlayHeight: number;
}

/**
 * App-global overlays mounted once after the shell layout: the shortcuts
 * reference, host import prompts, the command palette, the recent-sessions
 * switcher and the toast stack.
 */
export function AppShellOverlays({
  isEmbedded,
  serverInfo,
  sessionSearch,
  commandPaletteOpen,
  setCommandPaletteOpen,
  toggleLeftSidebar,
  toggleRightPanel,
  gridRoute,
  allConversations,
  serverConversationId,
  updateOverlayHeight,
}: AppShellOverlaysProps) {
  return (
    <>
      {/* Keyboard-shortcuts reference. Self-contained (owns its open state +
          ⌘/Ctrl+/ opener); ungated so it works on every route. */}
      <KeyboardShortcutsDialog />
      {/* Opens the import modal once per newly connected host. */}
      {!isEmbedded && <ImportReviewGate />}
      {/* Keep this after Outlet so a visible chat banner wins before the toast effect runs. */}
      {isFeatureEnabled(serverInfo, "arca_shutdown_warnings") && <ArcaShutdownToast />}
      {/* Dev-only `?import-preview` for the post-setup import modal. */}
      {ImportContextPreview && (
        <Suspense fallback={null}>
          <ImportContextPreview />
        </Suspense>
      )}
      {/* Global command palette (⌘K). Ungated so it works on every route
          and in embedded mode — the sidebar's "Search" button opens it
          there even though the ⌘K hotkey is disabled (it belongs to the
          host page). */}
      <CommandPalette
        key={sessionSearch ? "sessions" : "commands"}
        sessionsOnly={sessionSearch}
        open={commandPaletteOpen}
        onOpenChange={setCommandPaletteOpen}
        onToggleLeftSidebar={toggleLeftSidebar}
        onToggleRightSidebar={toggleRightPanel}
        rightSidebarShortcutEnabled={!gridRoute}
      />
      <RecentSessionsSwitcher
        conversations={allConversations}
        activeSessionId={serverConversationId ?? null}
        enabled={isElectronShell()}
      />
      {/* Transient toasts (e.g. "session archived"). Mounted once here so
          any surface can fire one via showToast(). */}
      {/* Match the previous toast system's effectively unbounded stack so
          security prompts cannot be hidden behind ordinary notifications. */}
      <Toaster
        position="top-center"
        visibleToasts={100}
        offset={{
          right: "1rem",
          bottom: updateOverlayToastOffset(updateOverlayHeight),
        }}
        mobileOffset={{
          right: "1rem",
          bottom: updateOverlayToastOffset(updateOverlayHeight),
        }}
      />
    </>
  );
}
