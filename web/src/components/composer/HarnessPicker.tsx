import {
  type ComponentProps,
  type KeyboardEvent,
  type ReactNode,
  type SyntheticEvent,
  createContext,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { ChevronLeftIcon, ChevronRightIcon } from "lucide-react";
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
const PickerInteractionContext = createContext<{
  pointerInteraction: boolean;
  showInitialSelection: boolean;
  onPointerInteraction: () => void;
  onKeyboardInteraction: () => void;
  closeMenu: () => void;
} | null>(null);

function usePickerInteraction() {
  const context = useContext(PickerInteractionContext);
  if (!context) throw new Error("Harness picker menus require a HarnessPicker");
  return context;
}

function useMenuInteractionProps(configOpen = false) {
  const interaction = usePickerInteraction();
  return {
    "data-input-method": interaction.pointerInteraction ? "pointer" : "keyboard",
    "data-initial-selection": interaction.showInitialSelection || undefined,
    onPointerEnter: interaction.onPointerInteraction,
    onPointerLeave: interaction.onPointerInteraction,
    onPointerDownCapture: interaction.onPointerInteraction,
    onPointerMoveCapture: (event: SyntheticEvent<HTMLElement>) => {
      interaction.onPointerInteraction();
      if (configOpen && event.currentTarget.contains(event.target as Node)) event.preventDefault();
    },
    onKeyDownCapture: (event: KeyboardEvent<HTMLElement>) => {
      interaction.onKeyboardInteraction();
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        interaction.closeMenu();
      }
    },
  };
}

function HarnessPickerContent({
  configOpen,
  menuOpen,
  onInitialSelectionFocus,
  onFocus,
  ...props
}: ComponentProps<typeof DropdownMenuContent> & {
  configOpen: boolean;
  menuOpen: boolean;
  onInitialSelectionFocus?: () => void;
}) {
  const interactionProps = useMenuInteractionProps(configOpen);
  const initialFocusHandled = useRef(false);
  const [content, setContent] = useState<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!menuOpen) initialFocusHandled.current = false;
  }, [menuOpen]);
  // Let the menu's focus scope pause a parent dialog before focusing portalled rows.
  useEffect(() => {
    if (!menuOpen || !content || initialFocusHandled.current) return;
    const selected = Array.from(
      content.querySelectorAll<HTMLElement>(
        '[role="menuitem"][data-active="true"]:not([data-disabled]):not([aria-disabled="true"])',
      ),
    ).find((item) => item.closest('[role="menu"]') === content);
    if (!selected) return;
    initialFocusHandled.current = true;
    selected.focus();
    onInitialSelectionFocus?.();
  }, [content, menuOpen, onInitialSelectionFocus]);
  return (
    <DropdownMenuContent
      {...props}
      {...interactionProps}
      ref={setContent}
      onFocus={(event) => {
        onFocus?.(event);
        if (
          event.defaultPrevented ||
          !menuOpen ||
          initialFocusHandled.current ||
          event.target !== event.currentTarget
        )
          return;
        initialFocusHandled.current = true;
        const focusedContent = event.currentTarget;
        const selected = Array.from(
          focusedContent.querySelectorAll<HTMLElement>(
            '[role="menuitem"][data-active="true"]:not([data-disabled]):not([aria-disabled="true"])',
          ),
        ).find((item) => item.closest('[role="menu"]') === focusedContent);
        if (!selected) return;
        selected.focus();
        // Keep Radix's entry-focus fallback from replacing the selected row.
        if (focusedContent.ownerDocument.activeElement === selected) event.preventDefault();
        onInitialSelectionFocus?.();
      }}
    />
  );
}

function focusSelectedMenuItem(content: HTMLElement): void {
  const items = Array.from(
    content.querySelectorAll<HTMLElement>(
      '[role="menuitemcheckbox"]:not([data-disabled]), [role="menuitem"]:not([data-disabled]):not([aria-disabled="true"])',
    ),
  ).filter((item) => item.closest('[role="menu"]') === content);
  const selected =
    items.find(
      (item) => item.getAttribute("aria-checked") === "true" || item.dataset.active === "true",
    ) ?? items[0];
  selected?.focus();
}

export function HarnessPickerSubContent({
  focusSelected = false,
  onSelectedFocus,
  ...props
}: ComponentProps<typeof DropdownMenuSubContent> & {
  focusSelected?: boolean;
  onSelectedFocus?: () => void;
}) {
  const interactionProps = useMenuInteractionProps();
  const [content, setContent] = useState<HTMLDivElement | null>(null);
  useLayoutEffect(() => {
    if (!focusSelected || !content) return;
    focusSelectedMenuItem(content);
    if (content.contains(content.ownerDocument.activeElement)) onSelectedFocus?.();
  }, [content, focusSelected, onSelectedFocus]);
  return <DropdownMenuSubContent {...props} {...interactionProps} ref={setContent} />;
}

export function HarnessPicker({
  open,
  onOpenChange,
  modal = false,
  trigger,
  tooltip,
  tooltipTestId,
  contentClassName,
  contentAlign = "end",
  testId,
  configOpen = false,
  onInitialSelectionFocus,
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
  onInitialSelectionFocus?: () => void;
  children: ReactNode;
}) {
  const guardedTooltip = useMenuGuardedTooltip(open);
  // Programmatic focus can retain :focus-visible during pointer-only interaction.
  const [pointerInteraction, setPointerInteraction] = useState(false);
  const [hasMenuInteraction, setHasMenuInteraction] = useState(false);
  useEffect(() => {
    if (!open) setHasMenuInteraction(false);
  }, [open]);
  useEffect(() => {
    if (open || !pointerInteraction) return;
    // Outside dismissal can leave focus elsewhere before Tab returns to the trigger.
    const onKeyDown = () => setPointerInteraction(false);
    document.addEventListener("keydown", onKeyDown, true);
    return () => document.removeEventListener("keydown", onKeyDown, true);
  }, [open, pointerInteraction]);
  const interaction = useMemo(
    () => ({
      pointerInteraction,
      showInitialSelection: open && pointerInteraction && !hasMenuInteraction,
      onPointerInteraction: () => {
        setPointerInteraction(true);
        setHasMenuInteraction(true);
      },
      onKeyboardInteraction: () => {
        setPointerInteraction(false);
        setHasMenuInteraction(true);
      },
      closeMenu: () => onOpenChange(false),
    }),
    [open, pointerInteraction, hasMenuInteraction, onOpenChange],
  );
  const menuTrigger = (
    <DropdownMenuTrigger asChild>
      <ComposerHarnessTrigger
        {...trigger}
        data-pointer-interaction={pointerInteraction || undefined}
        className={cn("data-[pointer-interaction=true]:focus-visible:ring-0", trigger.className)}
        onPointerDownCapture={(event) => {
          setPointerInteraction(true);
          trigger.onPointerDownCapture?.(event);
        }}
        onPointerMoveCapture={(event) => {
          if (open) setPointerInteraction(true);
          trigger.onPointerMoveCapture?.(event);
        }}
        onKeyDownCapture={(event) => {
          setPointerInteraction(false);
          trigger.onKeyDownCapture?.(event);
        }}
        onBlur={(event) => {
          if (!open) setPointerInteraction(false);
          trigger.onBlur?.(event);
        }}
      />
    </DropdownMenuTrigger>
  );
  return (
    <PickerInteractionContext.Provider value={interaction}>
      <DropdownMenu
        open={open}
        onOpenChange={(next) => {
          if (!next || !trigger.disabled) {
            if (next) setHasMenuInteraction(false);
            onOpenChange(next);
          }
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
        <HarnessPickerContent
          configOpen={configOpen}
          menuOpen={open}
          side="top"
          align={contentAlign}
          collisionPadding={12}
          avoidCollisions
          className={cn(HARNESS_MENU_CLASS_NAME, COMPOSER_HARNESS_MENU_SIZE, contentClassName)}
          data-testid={testId}
          onPointerDownOutside={interaction.onPointerInteraction}
          onInitialSelectionFocus={onInitialSelectionFocus}
        >
          {children}
        </HarnessPickerContent>
      </DropdownMenu>
    </PickerInteractionContext.Provider>
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
  editTestId,
  focusConfig = false,
  onConfigFocused,
  ...row
}: ComponentProps<typeof HarnessMenuRowContent> & {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSelect?: () => void;
  configContent?: ReactNode;
  editable?: boolean;
  isMobile?: boolean;
  disabled?: boolean;
  testId?: string;
  configTestId?: string;
  editTestId?: string;
  focusConfig?: boolean;
  onConfigFocused?: () => void;
}) {
  const { pointerInteraction, showInitialSelection, closeMenu } = usePickerInteraction();
  const allowConfigOpen = useRef(false);
  const showDetails = open || (showInitialSelection && row.active);
  const selectAndClose = () => {
    onSelect?.();
    closeMenu();
  };
  const isEditTarget = (target: EventTarget) =>
    target instanceof Element && target.closest("[data-harness-edit]") !== null;
  const rowProps = {
    className: "composer-agent-select min-w-0 flex-1 [&>svg]:hidden",
    "data-testid": testId,
    "data-active": row.active ? "true" : undefined,
    "aria-label": row.label,
    "aria-description": editable
      ? "Enter to select; Right Arrow to edit configuration."
      : undefined,
    textValue: row.label,
    disabled,
  };
  const content = (
    <>
      <HarnessMenuRowContent
        {...row}
        showDetails={showDetails}
        keyboardNavigation={!pointerInteraction}
      />
      {editable && (
        <span
          data-harness-edit=""
          data-testid={editTestId}
          aria-hidden="true"
          className={cn(
            "composer-agent-edit flex h-8 shrink-0 cursor-pointer items-center text-xs text-muted-foreground underline-offset-2 hover:text-foreground hover:underline",
            showDetails || isMobile
              ? "opacity-100"
              : cn(
                  "opacity-0",
                  pointerInteraction
                    ? "group-hover/agent:opacity-100"
                    : "group-focus-within/agent:opacity-100",
                ),
          )}
        >
          Edit
        </span>
      )}
    </>
  );
  return (
    <div
      className={HARNESS_MENU_ROW_CLASS_NAME}
      data-harness-menu-row=""
      data-active={row.active ? "true" : undefined}
      data-disabled={disabled ? "" : undefined}
    >
      {editable && !isMobile ? (
        <DropdownMenuSub
          open={open}
          onOpenChange={(next) => {
            // Let Radix handle grace-aware hover focus, but open config only on explicit intent.
            if (!next || allowConfigOpen.current) onOpenChange(next);
            allowConfigOpen.current = false;
          }}
        >
          <DropdownMenuSubTrigger
            {...rowProps}
            onPointerMove={() => {
              allowConfigOpen.current = false;
            }}
            onClick={(event) => {
              if (disabled) {
                event.preventDefault();
              } else if (isEditTarget(event.target)) {
                if (open) {
                  event.preventDefault();
                  onOpenChange(false);
                } else {
                  allowConfigOpen.current = true;
                }
              } else {
                event.preventDefault();
                selectAndClose();
              }
            }}
            onKeyDown={(event) => {
              if (event.target !== event.currentTarget) return;
              if (disabled) event.preventDefault();
              else if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                selectAndClose();
              } else if (event.key === "ArrowRight") {
                allowConfigOpen.current = !open;
              }
            }}
          >
            {content}
          </DropdownMenuSubTrigger>
          <HarnessPickerSubContent
            focusSelected={focusConfig}
            onSelectedFocus={onConfigFocused}
            className="composer-agent-menu composer-agent-config-menu max-h-[var(--radix-dropdown-menu-content-available-height)] w-[13.75rem] max-w-[calc(100vw-2rem)] overflow-y-auto p-2"
            data-testid={configTestId}
            onFocusOutside={(event) => {
              if (event.target instanceof Element && event.target.getAttribute("role") === "menu")
                event.preventDefault();
            }}
          >
            {configContent}
          </HarnessPickerSubContent>
        </DropdownMenuSub>
      ) : (
        <DropdownMenuItem
          {...rowProps}
          onSelect={() => onSelect?.()}
          onClick={(event) => {
            if (editable && !disabled && isEditTarget(event.target)) {
              event.preventDefault();
              onOpenChange(true);
            }
          }}
          onKeyDown={(event) => {
            if (
              editable &&
              !disabled &&
              event.target === event.currentTarget &&
              event.key === "ArrowRight"
            ) {
              event.preventDefault();
              onOpenChange(true);
            }
          }}
        >
          {content}
        </DropdownMenuItem>
      )}
    </div>
  );
}

export function HarnessPickerConfigRow({
  label,
  value,
  open,
  onOpenChange,
  isMobile,
  disabled,
  testId,
  valueTestId,
  configTestId,
  children,
}: {
  label: string;
  value: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  isMobile: boolean;
  disabled?: boolean;
  testId: string;
  valueTestId?: string;
  configTestId: string;
  children: ReactNode;
}) {
  const content = (
    <>
      <span className="flex-1">{label}</span>
      <span
        className="truncate text-right text-muted-foreground"
        data-testid={valueTestId}
        title={value}
      >
        {value}
      </span>
    </>
  );
  const rowProps = {
    className:
      "min-h-8 cursor-pointer rounded-lg px-2 text-13 data-disabled:pointer-events-none data-disabled:cursor-default data-disabled:opacity-50",
    "data-testid": testId,
    "aria-label": `${label}: ${value}`,
    textValue: label,
    disabled,
  };
  if (isMobile) {
    return (
      <DropdownMenuItem
        {...rowProps}
        onSelect={(event) => {
          event.preventDefault();
          onOpenChange(true);
        }}
      >
        {content}
        <ChevronRightIcon className="size-4" />
      </DropdownMenuItem>
    );
  }
  return (
    <DropdownMenuSub open={open} onOpenChange={onOpenChange}>
      <DropdownMenuSubTrigger
        {...rowProps}
        onPointerEnter={(event) => {
          if (event.pointerType !== "mouse" || disabled) return;
          // Direct row hover takes precedence over the previous submenu's pointer-grace area.
          event.currentTarget.focus();
          onOpenChange(true);
        }}
      >
        {content}
      </DropdownMenuSubTrigger>
      <HarnessPickerSubContent
        className="composer-agent-menu composer-agent-config-menu max-h-[var(--radix-dropdown-menu-content-available-height)] w-[13.75rem] max-w-[calc(100vw-2rem)] overflow-y-auto p-2"
        data-testid={configTestId}
        onFocusOutside={(event) => {
          if (event.target instanceof Element && event.target.getAttribute("role") === "menu")
            event.preventDefault();
        }}
      >
        {children}
      </HarnessPickerSubContent>
    </DropdownMenuSub>
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
