import { type ComponentProps, type ReactNode, useEffect, useRef, useState } from "react";
import { ChevronLeftIcon } from "lucide-react";
import { ComposerHarnessTrigger } from "./ComposerControls";
import {
  COMPOSER_HARNESS_MENU_SIZE,
  HARNESS_MENU_CLASS_NAME,
  HARNESS_MENU_ROW_CLASS_NAME,
  HarnessMenuRowContent,
} from "./HarnessMenuRow";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

type TriggerProps = ComponentProps<typeof ComposerHarnessTrigger> & { "data-testid"?: string };

export function HarnessPicker({
  open,
  onOpenChange,
  modal = true,
  trigger,
  tooltip,
  tooltipTestId,
  contentClassName,
  contentAlign = "end",
  testId,
  configOpen = false,
  children,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  modal?: boolean;
  trigger: TriggerProps;
  tooltip?: ReactNode;
  tooltipTestId?: string;
  contentClassName?: string;
  contentAlign?: "start" | "center" | "end";
  testId?: string;
  configOpen?: boolean;
  children: ReactNode;
}) {
  const guardedTooltip = useMenuGuardedTooltip(open);
  const menuTrigger = (
    <DropdownMenuTrigger asChild>
      <ComposerHarnessTrigger {...trigger} />
    </DropdownMenuTrigger>
  );
  return (
    <DropdownMenu
      open={open}
      onOpenChange={(next) => {
        if (!next || !trigger.disabled) onOpenChange(next);
      }}
      modal={modal}
    >
      {tooltip == null ? (
        menuTrigger
      ) : (
        <TooltipProvider>
          <Tooltip open={guardedTooltip.open} onOpenChange={guardedTooltip.onOpenChange}>
            <TooltipTrigger asChild>
              <span className="flex min-w-0" {...guardedTooltip.triggerProps}>
                {menuTrigger}
              </span>
            </TooltipTrigger>
            <TooltipContent
              side="top"
              className="max-w-80 flex-col items-start gap-0.5 px-3 py-2"
              data-testid={tooltipTestId}
            >
              {tooltip}
            </TooltipContent>
          </Tooltip>
        </TooltipProvider>
      )}
      <DropdownMenuContent
        side="top"
        align={contentAlign}
        collisionPadding={12}
        avoidCollisions
        className={cn(HARNESS_MENU_CLASS_NAME, COMPOSER_HARNESS_MENU_SIZE, contentClassName)}
        data-testid={testId}
        onPointerMoveCapture={(event) => {
          if (configOpen && event.currentTarget.contains(event.target as Node))
            event.preventDefault();
        }}
      >
        {children}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

export function HarnessPickerEntry({
  open,
  onOpenChange,
  onSelect,
  configContent,
  editable = true,
  isMobile = false,
  disabled,
  testId,
  configTestId,
  ...row
}: ComponentProps<typeof HarnessMenuRowContent> & {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSelect?: () => void;
  configContent?: ReactNode;
  disabled?: boolean;
  testId?: string;
  configTestId?: string;
}) {
  const rowContent = <HarnessMenuRowContent {...row} editable={editable} isMobile={isMobile} />;
  const rowProps = {
    className: cn(HARNESS_MENU_ROW_CLASS_NAME, row.active && "bg-muted"),
    "data-harness-menu-row": "",
    "data-active": row.active ? "true" : undefined,
    "data-testid": testId,
    disabled,
  };
  if (editable && !isMobile) {
    return (
      <DropdownMenuSub open={open} onOpenChange={onOpenChange}>
        <DropdownMenuSubTrigger {...rowProps} onPointerMove={(event) => event.preventDefault()}>
          {rowContent}
        </DropdownMenuSubTrigger>
        <DropdownMenuSubContent
          className="composer-agent-menu composer-agent-config-menu max-h-[var(--radix-dropdown-menu-content-available-height)] w-[13.75rem] max-w-[calc(100vw-2rem)] overflow-y-auto p-2"
          sideOffset={16}
          collisionPadding={12}
          data-testid={configTestId}
          onFocusOutside={(event) => {
            if (event.target instanceof Element && event.target.getAttribute("role") === "menu")
              event.preventDefault();
          }}
        >
          {configContent}
        </DropdownMenuSubContent>
      </DropdownMenuSub>
    );
  }
  return (
    <DropdownMenuItem
      {...rowProps}
      onSelect={(event) => {
        onSelect?.();
        if (editable) {
          event.preventDefault();
          onOpenChange(true);
        }
      }}
    >
      {rowContent}
    </DropdownMenuItem>
  );
}

export function HarnessPickerConfigPage({
  onBack,
  backTestId,
  testId,
  children,
}: {
  onBack: () => void;
  backTestId?: string;
  testId?: string;
  children: ReactNode;
}) {
  return (
    <div className="animate-in fade-in-0 slide-in-from-right-2 duration-150">
      <DropdownMenuItem
        data-testid={backTestId}
        className="items-center font-medium"
        onSelect={(event) => {
          event.preventDefault();
          onBack();
        }}
      >
        <ChevronLeftIcon className="size-4 shrink-0 opacity-70" /> Back
      </DropdownMenuItem>
      <DropdownMenuSeparator />
      <div data-testid={testId}>{children}</div>
    </div>
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
