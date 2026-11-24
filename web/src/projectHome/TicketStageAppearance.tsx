import { useId, useRef } from "react";
import * as RadioGroup from "radix-ui/radio-group";
import {
  CheckIcon,
  CircleCheckIcon,
  CircleDashedIcon,
  CircleDotIcon,
  CircleIcon,
  ClipboardListIcon,
  ClockIcon,
  EyeIcon,
  FlagIcon,
  PauseIcon,
  RocketIcon,
  RotateCcwIcon,
  SearchIcon,
  WrenchIcon,
  type LucideIcon,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { HoverCard, HoverCardContent, HoverCardTrigger } from "@/components/ui/hover-card";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import type { TicketStage, TicketStageColor, TicketStageIconName } from "@/lib/projectTicketsApi";
import { cn } from "@/lib/utils";

const COLORS = [
  { value: "gray", label: "Gray", text: "text-muted-foreground", tint: "bg-muted" },
  { value: "blue", label: "Blue", text: "text-session-active", tint: "bg-session-active/10" },
  {
    value: "purple",
    label: "Purple",
    text: "text-purple-600 dark:text-purple-400",
    tint: "bg-purple-500/10",
  },
  { value: "pink", label: "Pink", text: "text-brand-accent", tint: "bg-tag-pink" },
  { value: "red", label: "Red", text: "text-status-red", tint: "bg-status-red/10" },
  {
    value: "orange",
    label: "Orange",
    text: "text-orange-600 dark:text-orange-400",
    tint: "bg-orange-500/10",
  },
  { value: "amber", label: "Amber", text: "text-warning", tint: "bg-warning/15" },
  { value: "green", label: "Green", text: "text-success", tint: "bg-success/15" },
] as const satisfies readonly {
  value: TicketStageColor;
  label: string;
  text: string;
  tint: string;
}[];

const ICONS = [
  { value: "circle-dashed", label: "Dashed circle", component: CircleDashedIcon },
  { value: "circle", label: "Circle", component: CircleIcon },
  { value: "circle-dot", label: "Circle with dot", component: CircleDotIcon },
  { value: "circle-check", label: "Check", component: CircleCheckIcon },
  { value: "clipboard-list", label: "Checklist", component: ClipboardListIcon },
  { value: "eye", label: "Eye", component: EyeIcon },
  { value: "clock", label: "Clock", component: ClockIcon },
  { value: "pause", label: "Pause", component: PauseIcon },
  { value: "search", label: "Search", component: SearchIcon },
  { value: "flag", label: "Flag", component: FlagIcon },
  { value: "wrench", label: "Wrench", component: WrenchIcon },
  { value: "rocket", label: "Rocket", component: RocketIcon },
] as const satisfies readonly {
  value: TicketStageIconName;
  label: string;
  component: LucideIcon;
}[];

const DEFAULT_ICONS = new Map<string, TicketStageIconName>([
  ["open", "circle-dashed"],
  ["planning", "clipboard-list"],
  ["in_progress", "circle-dot"],
  ["review", "eye"],
]);

function appearance(stage?: TicketStage | null) {
  const defaultColor = stage?.is_complete
    ? "green"
    : stage?.id === "open"
      ? "gray"
      : stage?.id === "review"
        ? "amber"
        : "blue";
  const defaultIcon = stage?.is_complete
    ? "circle-check"
    : (DEFAULT_ICONS.get(stage?.id ?? "") ?? "circle-dot");
  return {
    color:
      COLORS.find((color) => color.value === stage?.color) ??
      COLORS.find((color) => color.value === defaultColor)!,
    icon:
      ICONS.find((icon) => icon.value === stage?.icon) ??
      ICONS.find((icon) => icon.value === defaultIcon)!,
  };
}

export function TicketStageIcon({
  stage,
  className,
}: {
  stage?: TicketStage | null;
  className?: string;
}) {
  const { color, icon } = appearance(stage);
  const Icon = icon.component;
  return <Icon aria-hidden className={cn("size-3.5 shrink-0", color.text, className)} />;
}

export function TicketStageBadge({
  stage,
  showInstructionsOnHover = false,
  className,
}: {
  stage: TicketStage;
  showInstructionsOnHover?: boolean;
  className?: string;
}) {
  const { color } = appearance(stage);
  const descriptionId = useId();
  const hasInstructions = showInstructionsOnHover && !!stage.instructions;
  const badge = (
    <Badge
      className={cn(
        "min-w-0 max-w-full shrink border-transparent",
        color.text,
        color.tint,
        hasInstructions && "cursor-help",
        className,
      )}
      title={hasInstructions ? undefined : stage.name}
      tabIndex={hasInstructions ? 0 : undefined}
      aria-describedby={hasInstructions ? descriptionId : undefined}
    >
      <TicketStageIcon stage={stage} />
      <span className="truncate">{stage.name}</span>
    </Badge>
  );
  if (!hasInstructions) return badge;
  return (
    <HoverCard openDelay={150} closeDelay={100}>
      <HoverCardTrigger asChild>{badge}</HoverCardTrigger>
      <HoverCardContent
        side="bottom"
        align="start"
        sideOffset={8}
        collisionPadding={8}
        className="max-h-(--radix-hover-card-content-available-height) w-72 max-w-(--radix-hover-card-content-available-width) overflow-y-auto wrap-anywhere px-3 py-2 text-sm"
      >
        <p className="font-medium">{stage.name}</p>
        <p id={descriptionId} className="mt-1 whitespace-pre-wrap text-muted-foreground">
          {stage.instructions}
        </p>
      </HoverCardContent>
    </HoverCard>
  );
}

export function TicketStageAppearancePicker({
  stage,
  disabled,
  onChange,
}: {
  stage: TicketStage;
  disabled: boolean;
  onChange: (fields: Pick<TicketStage, "color" | "icon">) => void;
}) {
  const { color, icon } = appearance(stage);
  const selectedColor = useRef<HTMLButtonElement>(null);
  const label = stage.name || "Unnamed stage";
  return (
    <TooltipProvider delayDuration={400}>
      <Popover>
        <Tooltip>
          <TooltipTrigger asChild>
            <span className="inline-flex shrink-0">
              <PopoverTrigger asChild>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  disabled={disabled}
                  aria-label={`Appearance for ${label}`}
                  className={cn("rounded-lg ring-1 ring-inset ring-foreground/5", color.tint)}
                >
                  <TicketStageIcon stage={stage} className="size-4" />
                </Button>
              </PopoverTrigger>
            </span>
          </TooltipTrigger>
          <TooltipContent>Change color and icon</TooltipContent>
        </Tooltip>
        <PopoverContent
          align="start"
          sideOffset={8}
          collisionPadding={12}
          aria-label={`Appearance for ${label}`}
          className="max-h-(--radix-popover-content-available-height) w-64 max-w-[calc(100vw-1.5rem)] gap-3 overflow-y-auto"
          onOpenAutoFocus={(event) => {
            event.preventDefault();
            selectedColor.current?.focus();
          }}
          onEscapeKeyDown={(event) => event.stopPropagation()}
        >
          <div className="flex min-w-0 items-center justify-between gap-2">
            <TicketStageBadge stage={{ ...stage, name: label }} />
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-xs"
                  aria-label="Reset appearance"
                  disabled={disabled || (!stage.color && !stage.icon)}
                  onClick={() => onChange({ color: null, icon: null })}
                >
                  <RotateCcwIcon aria-hidden />
                </Button>
              </TooltipTrigger>
              <TooltipContent>Reset appearance</TooltipContent>
            </Tooltip>
          </div>
          <div className="space-y-1.5">
            <p className="px-0.5 text-sm text-muted-foreground">Color</p>
            <RadioGroup.Root
              aria-label="Color"
              value={color.value}
              disabled={disabled}
              onValueChange={(value) => onChange({ color: value as TicketStageColor })}
              className="grid grid-cols-8 gap-0.5"
            >
              {COLORS.map((choice) => (
                <Tooltip key={choice.value}>
                  <TooltipTrigger asChild>
                    <RadioGroup.Item
                      ref={choice.value === color.value ? selectedColor : undefined}
                      value={choice.value}
                      aria-label={choice.label}
                      className={cn(
                        "flex aspect-square cursor-pointer items-center justify-center rounded-md outline-none transition-colors hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50",
                        choice.text,
                      )}
                    >
                      <span className="flex size-4.5 items-center justify-center rounded-full bg-current shadow-xs">
                        <RadioGroup.Indicator>
                          <CheckIcon aria-hidden className="size-3 text-white" strokeWidth={3} />
                        </RadioGroup.Indicator>
                      </span>
                    </RadioGroup.Item>
                  </TooltipTrigger>
                  <TooltipContent>{choice.label}</TooltipContent>
                </Tooltip>
              ))}
            </RadioGroup.Root>
          </div>
          <div className="space-y-1.5 border-t border-border/60 pt-2.5">
            <p className="px-0.5 text-sm text-muted-foreground">Icon</p>
            <RadioGroup.Root
              aria-label="Icon"
              value={icon.value}
              disabled={disabled}
              onValueChange={(value) => onChange({ icon: value as TicketStageIconName })}
              className="grid grid-cols-6 gap-1"
            >
              {ICONS.map((choice) => {
                const Icon = choice.component;
                return (
                  <Tooltip key={choice.value}>
                    <TooltipTrigger asChild>
                      <RadioGroup.Item
                        value={choice.value}
                        aria-label={choice.label}
                        className={cn(
                          "flex aspect-square cursor-pointer items-center justify-center rounded-md outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50",
                          icon.value === choice.value
                            ? cn(color.text, color.tint, "ring-1 ring-inset ring-current/30")
                            : "text-muted-foreground hover:bg-muted hover:text-foreground",
                        )}
                      >
                        <Icon aria-hidden className="size-4" />
                      </RadioGroup.Item>
                    </TooltipTrigger>
                    <TooltipContent>{choice.label}</TooltipContent>
                  </Tooltip>
                );
              })}
            </RadioGroup.Root>
          </div>
        </PopoverContent>
      </Popover>
    </TooltipProvider>
  );
}
