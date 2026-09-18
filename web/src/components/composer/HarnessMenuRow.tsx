import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

export const HARNESS_MENU_CLASS_NAME =
  "composer-agent-menu max-h-[var(--radix-dropdown-menu-content-available-height)] min-w-[17.5rem] max-w-[calc(100vw-2rem)] overflow-y-auto p-2";

export const COMPOSER_HARNESS_MENU_SIZE = "w-max min-w-[17.5rem]";

export const HARNESS_MENU_ROW_CLASS_NAME =
  "composer-agent-row group/agent relative flex min-h-8 w-full items-center rounded-lg";

export function PickerSectionHeader({ children }: { children: ReactNode }) {
  return (
    <div className="px-2 py-1 text-xs leading-5 font-normal text-muted-foreground">{children}</div>
  );
}

export function HarnessMenuRowContent({
  icon,
  label,
  summary,
  description,
  active,
  showDetails = false,
  keyboardNavigation = true,
  warning,
  summaryTestId,
}: {
  icon: ReactNode;
  label: string;
  summary: string;
  description?: string;
  active: boolean;
  showDetails?: boolean;
  keyboardNavigation?: boolean;
  warning?: ReactNode;
  summaryTestId?: string;
}) {
  const summaryVisibility = showDetails
    ? "opacity-100"
    : cn(
        "opacity-0",
        keyboardNavigation
          ? "group-focus-within/agent:opacity-100"
          : "group-hover/agent:opacity-100",
      );
  return (
    <span className="composer-agent-choice flex min-w-0 flex-1 items-center gap-2 py-1 pr-1 pl-2 text-[13px] leading-5">
      {icon}
      <span className={cn("flex min-w-0 items-center gap-1 text-left", active && "font-medium")}>
        <span className="truncate">{label}</span>
        {warning}
      </span>
      {description ? (
        <span className="relative min-w-0 flex-1 text-xs leading-5 text-muted-foreground">
          <span
            className={cn(
              "block truncate",
              showDetails
                ? "invisible"
                : keyboardNavigation
                  ? "group-focus-within/agent:invisible"
                  : "group-hover/agent:invisible",
            )}
          >
            {description}
          </span>
          <span className={cn("absolute inset-0 truncate text-right", summaryVisibility)}>
            {summary}
          </span>
        </span>
      ) : (
        <span
          data-testid={summaryTestId}
          title={summary}
          className={cn(
            "ml-auto min-w-0 flex-1 truncate text-right text-xs leading-4 text-muted-foreground",
            summaryVisibility,
          )}
        >
          {summary}
        </span>
      )}
    </span>
  );
}
