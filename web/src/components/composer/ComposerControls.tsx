import {
  forwardRef,
  useEffect,
  useRef,
  useState,
  type ComponentPropsWithoutRef,
  type ReactNode,
} from "react";
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
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { permissionModeConcept } from "@/lib/nativeHarnessModes";
import {
  COMPOSER_COLLAPSED_LABEL_CLASS,
  COMPOSER_CONTENT_INSET_CLASS,
  COMPOSER_TRAY_INSET_CLASS,
  COMPOSER_WORKSPACE_COLLAPSED_LABEL_CLASS,
  useCollapsedWorkspaceLabels,
} from "./ChatComposer";

/**
 * The workspace tray docked above the composer card. A tray nests inside the
 * card's outer edges by the shared inset rather than sharing its border box
 * (see the layout contract in ChatComposer); its chips sit on the card's
 * content inset line via the shared content inset; a chip's box starts there, and its
 * own px-1 keeps the glyph off the edge.
 */
export function ComposerWorkspaceBar({
  className,
  children,
  trailingContent,
  ...props
}: ComponentPropsWithoutRef<"div"> & { trailingContent?: ReactNode }) {
  const barRef = useRef<HTMLDivElement>(null);
  useCollapsedWorkspaceLabels(barRef);
  return (
    <div
      ref={barRef}
      className={cn(
        "composer-workspace-surface group/composer-workspace relative z-0 -mb-px flex h-7 min-w-0 items-center gap-0.5 rounded-t-2xl border border-b-0 border-border py-0.5 md:h-[37px] md:gap-2 md:py-1.5",
        COMPOSER_CONTENT_INSET_CLASS,
        COMPOSER_TRAY_INSET_CLASS,
        className,
      )}
      {...props}
    >
      {children}
      {trailingContent}
    </div>
  );
}

export const ComposerWorkspaceTrigger = forwardRef<
  HTMLButtonElement,
  ComponentPropsWithoutRef<"button"> & {
    kind: "directory" | "worktree";
    label: string;
    icon?: ReactNode;
    /** Hide the visible label; it stays the accessible name. */
    iconOnly?: boolean;
  }
>(function ComposerWorkspaceTrigger(
  { kind, label, icon, iconOnly = false, className, ...props },
  ref,
) {
  const Icon = kind === "directory" ? FolderIcon : GitForkIcon;
  return (
    <button
      ref={ref}
      type="button"
      // Icon-only while the bar is collapsed, so the label is the name.
      aria-label={label}
      className={cn(
        "relative inline-flex h-6 min-w-10 max-w-[calc(50%-0.25rem)] cursor-pointer items-center gap-1 rounded-md border border-transparent bg-transparent px-1 text-xs leading-4 font-normal text-muted-foreground transition-colors hover:bg-muted hover:text-foreground disabled:cursor-default disabled:opacity-50 md:min-w-11",
        className,
      )}
      {...props}
    >
      {icon ?? <Icon className="size-3.5 shrink-0" />}
      {!iconOnly && (
        <span
          data-workspace-collapse-label=""
          className={cn("min-w-0 truncate text-left", COMPOSER_WORKSPACE_COLLAPSED_LABEL_CLASS)}
        >
          {label}
        </span>
      )}
      <ChevronDownIcon className="size-3 shrink-0 opacity-60" />
    </button>
  );
});

export const ComposerHostTrigger = forwardRef<
  HTMLButtonElement,
  ComponentPropsWithoutRef<"button"> & {
    label: string;
    status?: "online" | "offline" | "unknown";
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
      className={cn(
        "flex h-8 w-11 shrink-0 cursor-pointer items-center justify-center gap-0.5 rounded-lg bg-transparent pl-1 pr-2 text-muted-foreground transition-colors hover:bg-muted/70 hover:text-foreground data-[state=open]:bg-muted data-[state=open]:text-foreground dark:hover:bg-muted/50 disabled:cursor-default md:h-7",
        className,
      )}
      {...props}
    >
      <span className="flex items-center">
        {status && (
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
        )}
        <Icon className="size-4 shrink-0" data-testid={`${testIdPrefix}-host-icon`} />
      </span>
    </button>
  );
});

export function ComposerPermissionPicker({
  label,
  value,
  harness,
  selectedValue,
  options,
  disabled = false,
  iconOnly = false,
  loading = false,
  interactiveWhileLoading = false,
  onSelect,
  testIdPrefix = "composer",
}: {
  label: string;
  value: string;
  harness?: string | null;
  selectedValue?: string | null;
  options: readonly { value: string; label: string }[];
  disabled?: boolean;
  /** Hide the visible value; it stays in the accessible name. */
  iconOnly?: boolean;
  loading?: boolean;
  interactiveWhileLoading?: boolean;
  onSelect: (value: string) => void;
  testIdPrefix?: string;
}) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild disabled={disabled || (loading && !interactiveWhileLoading)}>
        <button
          type="button"
          disabled={disabled || (loading && !interactiveWhileLoading)}
          aria-busy={loading || undefined}
          className={cn(
            "flex h-8 min-w-0 w-auto cursor-pointer items-center justify-center gap-1 rounded-lg bg-transparent px-2 text-foreground transition-colors hover:bg-muted/70 dark:hover:bg-muted/50 disabled:cursor-default disabled:opacity-50 md:h-7",
            loading && "disabled:opacity-100",
          )}
          aria-label={`${label}: ${value}`}
          title={`${label}: ${value}`}
          data-testid={`${testIdPrefix}-permission-chip`}
          data-permission-concept={permissionModeConcept(harness, selectedValue)}
        >
          <HandIcon className="size-3 shrink-0" />
          {!iconOnly && (
            <span
              className={cn("min-w-0 truncate text-ui font-normal", COMPOSER_COLLAPSED_LABEL_CLASS)}
            >
              {value}
            </span>
          )}
          <ChevronDownIcon className="size-4 shrink-0 opacity-60" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="start"
        className="w-max min-w-[13.75rem] max-w-[calc(100vw-2rem)]"
        data-testid={`${testIdPrefix}-permission-menu`}
        onCloseAutoFocus={(event) => {
          // A closing animation must not steal focus from a newly opened menu.
          if (document.activeElement?.closest('[role="menu"][data-state="open"]')) {
            event.preventDefault();
          }
        }}
      >
        <div className="px-2 py-1 text-xs text-muted-foreground">{label}</div>
        <DropdownMenuRadioGroup value={selectedValue ?? undefined} onValueChange={onSelect}>
          {options.map((option) => (
            <DropdownMenuRadioItem
              key={option.value}
              value={option.value}
              data-testid={`${testIdPrefix}-permission-option-${option.value}`}
              data-permission-concept={permissionModeConcept(harness, option.value)}
              className="whitespace-normal break-words"
            >
              {option.label}
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
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

const MENU_CLOSE_TOOLTIP_GUARD_MS = 600;

function useMenuGuardedTooltip(menuOpen: boolean) {
  const [wantsOpen, setWantsOpen] = useState(false);
  // Epoch millis until which open requests are ignored; Infinity while the
  // menu is open. A ref, not state: it is only read when Radix requests an
  // open, so changing it never needs a re-render.
  const suppressedUntil = useRef(0);
  useEffect(() => {
    if (menuOpen) {
      setWantsOpen(false);
      suppressedUntil.current = Number.POSITIVE_INFINITY;
    } else if (suppressedUntil.current === Number.POSITIVE_INFINITY) {
      suppressedUntil.current = Date.now() + MENU_CLOSE_TOOLTIP_GUARD_MS;
    }
  }, [menuOpen]);
  return {
    open: wantsOpen && !menuOpen,
    onOpenChange: (next: boolean) =>
      setWantsOpen(next && !menuOpen && Date.now() >= suppressedUntil.current),
    triggerProps: {
      onPointerEnter: () => {
        if (!menuOpen) suppressedUntil.current = 0;
      },
    },
  } as const;
}

/**
 * Hover tooltip for a composer control that opens a menu. It stays hidden
 * while the menu is open and doesn't reopen as focus returns on close.
 */
export function ComposerMenuTooltip({
  menuOpen,
  content,
  variant = "default",
  testId,
  className,
  contentClassName,
  children,
}: {
  menuOpen: boolean;
  content: ReactNode;
  variant?: "default" | "session-info";
  testId?: string;
  /** Classes for the wrapper that receives the hover. */
  className?: string;
  /** Classes for the tooltip surface. */
  contentClassName?: string;
  children: ReactNode;
}) {
  const guardedTooltip = useMenuGuardedTooltip(menuOpen);
  return (
    <TooltipProvider>
      <Tooltip open={guardedTooltip.open} onOpenChange={guardedTooltip.onOpenChange}>
        <TooltipTrigger asChild>
          <span className={cn("flex min-w-0", className)} {...guardedTooltip.triggerProps}>
            {children}
          </span>
        </TooltipTrigger>
        <TooltipContent
          side="top"
          className={cn(
            "max-w-80 flex-col items-start gap-0.5 px-3 py-2",
            variant === "session-info" &&
              "w-64 max-w-[calc(100vw-2rem)] items-stretch rounded-lg bg-popover p-2.5 text-popover-foreground whitespace-normal shadow-menu ring-1 ring-foreground/10",
            contentClassName,
          )}
          data-testid={testId}
        >
          {content}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

export const ComposerHarnessTrigger = forwardRef<
  HTMLButtonElement,
  Omit<ComponentPropsWithoutRef<typeof Button>, "children"> & {
    label: string;
    model: string;
    loading?: boolean;
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
    loading = false,
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
      aria-busy={loading || undefined}
      className={cn(
        "h-auto min-h-8 min-w-0 w-auto max-w-full gap-1 rounded-lg border-0 px-2 py-0 text-[13px] leading-5 font-normal text-muted-foreground hover:text-foreground md:min-h-7",
        className,
      )}
      {...props}
    >
      {icon}
      {loading && (
        <Loader2Icon
          role="status"
          className="size-3 shrink-0 animate-spin"
          aria-label="Loading model"
          data-testid={`${testIdPrefix}-model-loading`}
        />
      )}
      {pending && !loading && (
        <Loader2Icon
          className="size-3 shrink-0 animate-spin"
          aria-label="Model change pending"
          data-testid={`${testIdPrefix}-model-pending`}
        />
      )}
      {/* Without an icon the text is the only identification, so it stays put
          when the row collapses. */}
      <span
        className={cn(
          "inline-flex min-w-0 flex-nowrap items-baseline gap-1",
          icon != null && COMPOSER_COLLAPSED_LABEL_CLASS,
          labelClassName,
        )}
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
