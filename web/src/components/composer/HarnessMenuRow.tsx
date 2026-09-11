import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

export const HARNESS_MENU_CLASS_NAME =
  "composer-agent-menu max-h-[var(--radix-dropdown-menu-content-available-height)] min-w-[17.5rem] max-w-[calc(100vw-2rem)] overflow-y-auto p-2";

export const COMPOSER_HARNESS_MENU_SIZE = "w-[17.5rem] min-w-0";

export const HARNESS_MENU_ROW_CLASS_NAME =
  "composer-agent-row group/agent relative flex min-h-8 w-full items-center gap-1 rounded-lg pr-3 transition-colors hover:bg-muted focus:bg-muted [&>svg]:hidden";

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
  editable = true,
  isMobile = false,
  warning,
  summaryTestId,
  editTestId,
}: {
  icon: ReactNode;
  label: string;
  summary: string;
  description?: string;
  active: boolean;
  editable?: boolean;
  isMobile?: boolean;
  warning?: ReactNode;
  summaryTestId?: string;
  editTestId?: string;
}) {
  const summaryVisibility = active
    ? "opacity-100"
    : "opacity-0 group-hover/agent:opacity-100 group-focus-within/agent:opacity-100";
  return (
    <>
      <span className="composer-agent-choice flex min-w-0 flex-1 items-center gap-2 py-1 pr-0 pl-2 text-[13px] leading-5">
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
                active
                  ? "invisible"
                  : "group-hover/agent:invisible group-focus-within/agent:invisible",
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
            className={cn(
              "ml-auto min-w-0 flex-1 whitespace-normal break-words text-right text-xs leading-4 text-muted-foreground",
              summaryVisibility,
            )}
          >
            {summary}
          </span>
        )}
      </span>
      {editable && (
        <span
          aria-label={`Edit ${label} configuration`}
          data-testid={editTestId}
          className={cn(
            "composer-agent-edit flex h-8 shrink-0 cursor-pointer items-center rounded-none px-0 py-0 text-xs leading-4 text-muted-foreground focus:bg-transparent data-open:bg-transparent [&>svg]:hidden",
            summaryVisibility,
            isMobile && "opacity-100",
          )}
        >
          Edit
        </span>
      )}
    </>
  );
}
