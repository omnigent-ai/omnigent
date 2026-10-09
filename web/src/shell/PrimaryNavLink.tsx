import type { MouseEvent, ReactNode } from "react";
import type { LucideIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Link } from "@/lib/routing";
import { cn } from "@/lib/utils";
import { SIDEBAR_ROW } from "./sidebarStyles";

const HOVER_HIGHLIGHT = "hover:bg-muted hover:text-foreground dark:hover:bg-muted/50";
const ACTIVE_HIGHLIGHT =
  "bg-[var(--sidebar-active)] text-[var(--sidebar-active-foreground)] hover:bg-[var(--sidebar-active)] hover:text-[var(--sidebar-active-foreground)] dark:hover:bg-[var(--sidebar-active)] dark:hover:text-[var(--sidebar-active-foreground)]";

export interface PrimaryNavLinkProps {
  to: string;
  label: string;
  icon: LucideIcon;
  active: boolean;
  componentId: string;
  testId?: string;
  onClick?: (event: MouseEvent<HTMLAnchorElement>) => void;
  trailing?: ReactNode;
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
}: PrimaryNavLinkProps) {
  return (
    <Button
      asChild
      variant="ghost"
      className={cn(
        SIDEBAR_ROW,
        "w-full justify-start border-0 font-normal",
        HOVER_HIGHLIGHT,
        active && ACTIVE_HIGHLIGHT,
      )}
      data-testid={testId}
    >
      <Link
        to={to}
        onClick={onClick}
        componentId={componentId}
        aria-current={active ? "page" : undefined}
      >
        <Icon
          className={cn(
            "ui-icon",
            active ? "text-[var(--sidebar-active-foreground)]" : "text-muted-foreground",
          )}
        />
        {label}
        {trailing}
      </Link>
    </Button>
  );
}
