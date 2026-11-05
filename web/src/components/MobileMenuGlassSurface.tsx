import { useCallback, useState } from "react";
import { createPortal } from "react-dom";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { OmniGlassSurface } from "./OmniGlassSurface";

/** Keep glass outside the scrolling menu content when Radix provides a positioning wrapper. */
export function MobileMenuGlassSurface() {
  const isMobile = useIsMobileViewport();
  const [placement, setPlacement] = useState<{ target: HTMLElement | null; radius: number }>({
    target: null,
    radius: 12,
  });
  const registerSurface = useCallback((element: HTMLSpanElement | null) => {
    const content = element?.parentElement;
    if (!content) return;
    const wrapper = content.parentElement;
    setPlacement({
      target: wrapper?.hasAttribute("data-radix-popper-content-wrapper") ? wrapper : null,
      radius: Number.parseFloat(getComputedStyle(content).borderTopLeftRadius) || 12,
    });
  }, []);
  if (!isMobile) return null;
  const surface = (
    <OmniGlassSurface
      variant="menu"
      borderRadius={placement.radius}
      className="mobile-menu-glass-layer -z-10"
    />
  );
  return (
    <>
      <span ref={registerSurface} hidden />
      {placement.target ? createPortal(surface, placement.target) : surface}
    </>
  );
}
