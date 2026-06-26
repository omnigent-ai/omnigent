import { cn } from "@/lib/utils";

/**
 * Cold-start skeleton of the app shell.
 *
 * Rendered by ``App`` while the ``/v1/info`` boot probe is in flight
 * (``info === "loading"``) in place of the old ``return null`` — so the
 * very first paint shows the shell's silhouette (left conversations rail +
 * top header + a central content placeholder) instead of a blank white
 * screen. ``main.tsx`` now mounts the React tree immediately and flips
 * ``info`` from ``"loading"`` to the resolved value when the probe settles,
 * so this is what the user sees during that window.
 *
 * Geometry mirrors ``AppShell`` / ``Sidebar`` / ``ChatHeader`` — the 320px
 * (``w-80``) desktop rail and the 56px (``h-14``) header — so swapping in the
 * real tree on probe-resolve doesn't shift the layout (no CLS jump). On
 * mobile the real sidebar is an off-screen overlay, so the rail is hidden
 * here too and only the main column shows, matching the real first paint.
 *
 * Every surface is token-backed (``bg-card`` / ``border-border`` /
 * ``bg-muted-foreground/20``) so the skeleton tracks both the light and dark
 * themes with no hardcoded colors. The placeholder pulse is gated behind
 * ``motion-safe:`` so users with ``prefers-reduced-motion`` get a static
 * silhouette instead of an animation.
 */

/** One shimmering placeholder block. Decorative — hidden from assistive tech. */
function Block({ className }: { className?: string }) {
  return (
    <div
      aria-hidden
      className={cn("rounded-md bg-muted-foreground/20 motion-safe:animate-pulse", className)}
    />
  );
}

// Stable ids (not array indices) keep the lists lint-clean and let each row
// carry its own placeholder width so the rail reads like a real list.
const RAIL_ROWS = [
  { id: "rail-a", width: "w-4/5" },
  { id: "rail-b", width: "w-3/5" },
  { id: "rail-c", width: "w-11/12" },
  { id: "rail-d", width: "w-2/3" },
  { id: "rail-e", width: "w-3/4" },
  { id: "rail-f", width: "w-1/2" },
  { id: "rail-g", width: "w-5/6" },
  { id: "rail-h", width: "w-3/5" },
] as const;

export function AppShellSkeleton() {
  return (
    <div
      className="app-shell relative flex h-dvh bg-sidebar text-foreground"
      role="status"
      aria-busy="true"
    >
      <span className="sr-only">Loading…</span>

      {/* Left conversations rail — desktop floating card (mirrors Sidebar's
          md:m-2 md:w-[var(--sidebar-width)] floating treatment, default 320px).
          Hidden on mobile, where the real sidebar starts as an off-screen
          overlay. */}
      <aside
        aria-hidden
        className="hidden md:m-2 md:flex md:w-80 md:flex-col md:gap-5 md:rounded-xl md:border md:border-border md:bg-card md:p-4 md:shadow-lg"
      >
        {/* Brand row + top controls (Omnigent label, inbox, collapse). */}
        <div className="flex items-center justify-between">
          <Block className="h-5 w-28" />
          <div className="flex items-center gap-1.5">
            <Block className="size-7 rounded-full" />
            <Block className="size-7 rounded-full" />
          </div>
        </div>

        {/* New-session button. */}
        <Block className="h-9 w-full rounded-lg" />

        {/* Conversation rows. */}
        <div className="flex flex-col gap-3.5">
          {RAIL_ROWS.map((row) => (
            <div key={row.id} className="flex h-6 items-center">
              <Block className={cn("h-3.5", row.width)} />
            </div>
          ))}
        </div>
      </aside>

      {/* Content region (everything right of the rail). */}
      <div className="relative flex min-h-0 min-w-0 flex-1 flex-col">
        {/* Header band — same 56px height as ChatHeader so the body below
            starts at the same offset the real chrome leaves it. */}
        <div aria-hidden className="flex h-14 shrink-0 items-center justify-between px-3">
          <div className="flex items-center gap-2">
            {/* Sidebar-open affordance — mobile only, like ChatHeader's. */}
            <Block className="size-8 rounded-md md:hidden" />
            <Block className="size-5 rounded" />
            <Block className="h-4 w-36" />
          </div>
          <div className="flex items-center gap-2">
            <Block className="h-8 w-24 rounded-full" />
            <Block className="size-8 rounded-md" />
          </div>
        </div>

        {/* Central placeholder — a centered greeting + composer, approximating
            the landing/chat column so the real content lands in roughly the
            same place. */}
        <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-8 px-4 pb-6">
          <div className="flex w-full max-w-2xl flex-col items-center gap-3">
            <Block className="h-6 w-48" />
            <Block className="h-4 w-full max-w-md" />
          </div>
          <Block className="h-32 w-full max-w-2xl rounded-2xl" />
        </div>
      </div>
    </div>
  );
}
