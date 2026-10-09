import type { MouseEvent, ReactNode } from "react";
import type { LucideIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
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
  compact?: boolean;
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
  compact = false,
}: PrimaryNavLinkProps) {
  const link = (
    <Button
      asChild
      variant="ghost"
      className={cn(
        compact ? "size-8 justify-center border-0 p-0" : SIDEBAR_ROW,
        !compact && "w-full justify-start border-0 font-normal",
        HOVER_HIGHLIGHT,
        active && ACTIVE_HIGHLIGHT,
      )}
      data-testid={testId}
    >
      <Link
        to={to}
        onClick={onClick}
        componentId={componentId}
        aria-label={compact ? label : undefined}
        aria-current={active ? "page" : undefined}
      >
        <Icon
          className={cn(
            "ui-icon",
            active ? "text-[var(--sidebar-active-foreground)]" : "text-muted-foreground",
          )}
        />
        {!compact && label}
        {!compact && trailing}
      </Link>
    </Button>
  );
  return compact ? (
    <Tooltip>
      <TooltipTrigger asChild>{link}</TooltipTrigger>
      <TooltipContent side="right">{label}</TooltipContent>
    </Tooltip>
  ) : (
    link
  );
}
