import { forwardRef, type ComponentPropsWithoutRef, type ReactNode } from "react";
import {
  ChevronDownIcon,
  FolderIcon,
  GitForkIcon,
  HandIcon,
  LaptopIcon,
  Loader2Icon,
  MonitorCloudIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";

export function ComposerWorkspaceBar({ className, ...props }: ComponentPropsWithoutRef<"div">) {
  return (
    <div
      className={cn(
        "relative z-0 mx-3 -mb-px flex h-[37px] min-w-0 items-start gap-2 rounded-t-2xl border border-b-0 border-border bg-muted/70 px-2 pt-1.5",
        className,
      )}
      {...props}
    />
  );
}

export const ComposerWorkspaceTrigger = forwardRef<
  HTMLButtonElement,
  ComponentPropsWithoutRef<"button"> & { kind: "directory" | "worktree"; label: string }
>(function ComposerWorkspaceTrigger({ kind, label, className, ...props }, ref) {
  const Icon = kind === "directory" ? FolderIcon : GitForkIcon;
  return (
    <button
      ref={ref}
      type="button"
      className={cn(
        "relative inline-flex h-6 min-w-0 max-w-[calc(50%-0.25rem)] cursor-pointer items-center gap-1 rounded-md border border-transparent bg-transparent px-1 text-xs leading-4 font-normal text-muted-foreground transition-colors hover:bg-muted hover:text-foreground disabled:cursor-default disabled:opacity-50",
        className,
      )}
      {...props}
    >
      <Icon className="size-3.5 shrink-0" />
      <span className="min-w-0 truncate text-left">{label}</span>
      <ChevronDownIcon className="size-3 shrink-0 opacity-60" />
    </button>
  );
});

export const ComposerHostTrigger = forwardRef<
  HTMLButtonElement,
  ComponentPropsWithoutRef<"button"> & {
    label: string;
    status: "online" | "offline" | "unknown";
    cloud?: boolean;
    testIdPrefix?: string;
  }
>(function ComposerHostTrigger(
  { label, status, cloud = false, testIdPrefix = "composer", className, ...props },
  ref,
) {
  const Icon = cloud ? MonitorCloudIcon : LaptopIcon;
  return (
    <button
      ref={ref}
      type="button"
      aria-label={label}
      title={label}
      className={cn(
        "flex h-8 w-11 shrink-0 cursor-pointer items-center justify-center gap-0.5 rounded-lg bg-transparent pl-1 pr-2 text-muted-foreground transition-colors hover:bg-muted/70 hover:text-foreground data-[state=open]:bg-muted data-[state=open]:text-foreground dark:hover:bg-muted/50 disabled:cursor-default md:h-7",
        className,
      )}
      {...props}
    >
      <span className="flex items-center">
        <span className="flex size-4 shrink-0 items-center justify-center">
          <span
            aria-hidden
            className={cn(
              "size-2 rounded-full",
              status === "online" ? "bg-success" : "border-[1.5px] border-muted-foreground",
            )}
            data-testid={`${testIdPrefix}-host-status`}
          />
        </span>
        <Icon className="size-4 shrink-0" data-testid={`${testIdPrefix}-host-icon`} />
      </span>
    </button>
  );
});

export function ComposerPermissionPicker({
  label,
  value,
  options,
  disabled = false,
  onSelect,
  testIdPrefix = "composer",
}: {
  label: string;
  value: string;
  options: readonly { value: string; label: string }[];
  disabled?: boolean;
  onSelect: (value: string) => void;
  testIdPrefix?: string;
}) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          disabled={disabled}
          className="flex h-8 min-w-0 w-auto cursor-pointer items-center justify-center gap-1 rounded-lg bg-transparent px-2 text-foreground transition-colors hover:bg-muted/70 dark:hover:bg-muted/50 disabled:cursor-default disabled:opacity-50 md:h-7"
          aria-label={`${label}: ${value}`}
          title={`${label}: ${value}`}
          data-testid={`${testIdPrefix}-permission-chip`}
        >
          <HandIcon className="size-3 shrink-0" />
          <span className="min-w-0 truncate text-ui font-normal">{value}</span>
          <ChevronDownIcon className="size-4 shrink-0 opacity-60" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="start"
        className="w-max min-w-[13.75rem] max-w-[calc(100vw-2rem)]"
        data-testid={`${testIdPrefix}-permission-menu`}
      >
        <div className="px-2 py-1 text-xs text-muted-foreground">{label}</div>
        {options.map((option) => (
          <DropdownMenuItem
            key={option.value}
            onSelect={() => onSelect(option.value)}
            data-testid={`${testIdPrefix}-permission-option-${option.value}`}
            className="whitespace-normal break-words"
          >
            {option.label}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/**
 * Label/value rows for a composer trigger's config tooltip. The bold key
 * separates each row's label from its value.
 */
export function ComposerConfigTooltipRows({
  rows,
}: {
  rows: readonly { label: string; value: string }[];
}) {
  return (
    <>
      {rows.map((row) => (
        <span key={row.label}>
          <span className="font-semibold">{row.label}:</span> {row.value}
        </span>
      ))}
    </>
  );
}

export const ComposerHarnessTrigger = forwardRef<
  HTMLButtonElement,
  Omit<ComponentPropsWithoutRef<typeof Button>, "children"> & {
    label: string;
    model: string;
    pending?: boolean;
    effort?: string;
    icon?: ReactNode;
    testIdPrefix?: string;
    labelClassName?: string;
  }
>(function ComposerHarnessTrigger(
  {
    label,
    model,
    effort,
    icon,
    pending = false,
    testIdPrefix = "composer",
    labelClassName,
    className,
    ...props
  },
  ref,
) {
  return (
    <Button
      ref={ref}
      type="button"
      variant="ghost"
      size="sm"
      aria-label={label}
      className={cn(
        "h-auto min-h-8 min-w-0 w-auto max-w-full gap-1 rounded-lg border-0 px-2 py-0 text-[13px] leading-5 font-normal text-muted-foreground hover:text-foreground md:min-h-7",
        className,
      )}
      {...props}
    >
      {icon}
      {pending && (
        <Loader2Icon
          className="size-3 shrink-0 animate-spin"
          aria-label="Model change pending"
          data-testid={`${testIdPrefix}-model-pending`}
        />
      )}
      <span
        className={cn("inline-flex min-w-0 flex-nowrap items-baseline gap-1", labelClassName)}
        data-testid={`${testIdPrefix}-agent-config-value`}
      >
        {model && (
          <span
            className="min-w-0 truncate text-left text-[13px] leading-5 font-medium text-foreground"
            title={model}
            data-testid={`${testIdPrefix}-agent-model-value`}
          >
            {model}
          </span>
        )}
        {effort && (
          <span
            className="shrink-0 text-[13px] leading-5 font-normal text-muted-foreground"
            data-testid={`${testIdPrefix}-agent-effort-value`}
          >
            {effort}
          </span>
        )}
      </span>
      <ChevronDownIcon className="size-4 shrink-0 opacity-60" />
    </Button>
  );
});
