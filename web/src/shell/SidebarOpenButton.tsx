import { ArrowLeftIcon, MenuIcon, PanelLeftIcon } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";

/** Shared collapsed-sidebar control for page and conversation headers. */
export function SidebarOpenButton({
  onOpenSidebar,
  settingsMode = false,
  componentId,
}: {
  onOpenSidebar: (peek?: boolean) => void;
  settingsMode?: boolean;
  componentId: string;
}) {
  const isMobile = useIsMobileViewport();
  const peekTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const peekRequest = useRef(0);
  const suppressTooltip = useRef(false);
  const [tooltipOpen, setTooltipOpen] = useState(false);
  const cancelPeek = useCallback(() => {
    peekRequest.current += 1;
    if (peekTimer.current) {
      clearTimeout(peekTimer.current);
      peekTimer.current = null;
    }
    suppressTooltip.current = false;
  }, []);
  const onPeekSidebar = useCallback(() => {
    // A mobile tap's synthetic pointerenter must not open a hover preview.
    if (isMobile) return;
    cancelPeek();
    const request = peekRequest.current;
    peekTimer.current = setTimeout(() => {
      peekTimer.current = null;
      if (peekRequest.current !== request) return;
      // The peek replaces the hover tooltip; keyboard focus still shows it.
      suppressTooltip.current = true;
      setTooltipOpen(false);
      onOpenSidebar(true);
    }, 400);
  }, [isMobile, onOpenSidebar, cancelPeek]);
  useEffect(() => cancelPeek, [cancelPeek]);

  const label = settingsMode ? "Back to settings menu" : "Open sidebar";
  return (
    <Tooltip
      open={tooltipOpen}
      onOpenChange={(next) => {
        if (next && suppressTooltip.current) return;
        setTooltipOpen(next);
      }}
    >
      <TooltipTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="icon-xs"
          aria-label={label}
          componentId={componentId}
          onClick={() => {
            cancelPeek();
            onOpenSidebar(false);
          }}
          // macOS uses the persistent titlebar cluster in place of this button.
          className="chat-header-sidebar-toggle pointer-events-auto border-none text-muted-foreground hover:text-foreground max-md:size-11"
          onPointerEnter={onPeekSidebar}
          onPointerDown={cancelPeek}
          onPointerLeave={cancelPeek}
        >
          {settingsMode ? (
            <ArrowLeftIcon className="size-4 max-md:size-5" />
          ) : isMobile ? (
            <MenuIcon className="size-5" />
          ) : (
            <PanelLeftIcon className="size-4" />
          )}
        </Button>
      </TooltipTrigger>
      <TooltipContent side="bottom">{label}</TooltipContent>
    </Tooltip>
  );
}
