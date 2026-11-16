import type { MouseEvent, ReactNode } from "react";
import type { LucideIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Link } from "@/lib/routing";
import { cn } from "@/lib/utils";
import { MOBILE_GLASS_SURFACE } from "./mobileGlass";
import { SIDEBAR_ROW } from "./sidebarStyles";

const HOVER_HIGHLIGHT = "hover:bg-muted hover:text-foreground dark:hover:bg-muted/50";
const ACTIVE_HIGHLIGHT =
  "bg-[var(--sidebar-active)] text-[var(--sidebar-active-foreground)] hover:bg-[var(--sidebar-active)] hover:text-[var(--sidebar-active-foreground)] dark:hover:bg-[var(--sidebar-active)] dark:hover:text-[var(--sidebar-active-foreground)]";

/** Full-width sidebar rows, or the icon-over-label cells of the phone drawer's strip. */
export type PrimaryNavLayout = "row" | "cell";

// Equal-width cells at least 44px tall.
const CELL =
  "h-auto min-h-11 min-w-0 flex-1 basis-0 flex-col gap-1 rounded-xl border-0 px-0.5 py-2.5 font-normal mobile-touch-target";

/** On the phone drawer, wraps the always-on destinations in one strip; elsewhere they stay rows. */
export function PrimaryNavStrip({
  layout,
  children,
}: {
  layout: PrimaryNavLayout;
  children: ReactNode;
}) {
  if (layout === "row") return children;
  return (
    <div
      data-testid="sidebar-primary-nav-strip"
      className={cn("mb-1 flex gap-1 rounded-2xl p-1", MOBILE_GLASS_SURFACE)}
    >
      {children}
    </div>
  );
}

export interface PrimaryNavLinkProps {
  to: string;
  label: string;
  icon: LucideIcon;
  active: boolean;
  componentId: string;
  testId?: string;
  onClick?: (event: MouseEvent<HTMLAnchorElement>) => void;
  trailing?: ReactNode;
  layout?: PrimaryNavLayout;
  /** Visible label in a cell; the accessible name stays `label`. */
  shortLabel?: string;
  className?: string;
  ariaKeyShortcuts?: string;
}

export function PrimaryNavLink({
  to,
  label,
  icon: Icon,
  active,
  componentId,
  testId,
  onClick,
  trailing,
  layout = "row",
  shortLabel,
  className,
  ariaKeyShortcuts,
}: PrimaryNavLinkProps) {
  const cell = layout === "cell";
  const visibleLabel = cell ? (shortLabel ?? label) : label;
  return (
    <Button
      asChild
      variant="ghost"
      className={cn(
        cell
          ? CELL
          : [SIDEBAR_ROW, "w-full justify-start border-0 font-normal mobile-touch-target"],
        HOVER_HIGHLIGHT,
        active && ACTIVE_HIGHLIGHT,
        className,
      )}
      data-testid={testId}
    >
      <Link
        to={to}
        onClick={onClick}
        componentId={componentId}
        aria-current={active ? "page" : undefined}
        aria-label={visibleLabel === label ? undefined : label}
        aria-keyshortcuts={ariaKeyShortcuts}
      >
        <Icon
          className={cn(
            "ui-icon",
            active ? "text-[var(--sidebar-active-foreground)]" : "text-muted-foreground",
          )}
        />
        {cell ? (
          // 4px under the Appearance interface size (11px at the phone default).
          <span className="max-w-full truncate text-[calc(var(--text-ui)-4px)] leading-tight tracking-tight">
            {visibleLabel}
          </span>
        ) : (
          label
        )}
        {trailing}
      </Link>
    </Button>
  );
}
