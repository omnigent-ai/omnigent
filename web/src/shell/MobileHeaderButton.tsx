import { forwardRef, type ComponentPropsWithoutRef } from "react";
import { OmniGlassSurface } from "@/components/OmniGlassSurface";
import { Button } from "@/components/ui/button";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { cn } from "@/lib/utils";

/** Keep the native button as the Radix trigger and layer glass behind its icon. */
export const MobileHeaderButton = forwardRef<
  HTMLButtonElement,
  Omit<ComponentPropsWithoutRef<typeof Button>, "asChild">
>(function MobileHeaderButton({ children, className, size, variant, ...props }, ref) {
  const isMobile = useIsMobileViewport();
  return (
    <Button
      {...props}
      ref={ref}
      size={isMobile ? "icon" : size}
      variant={isMobile ? "ghost" : variant}
      className={cn(
        className,
        isMobile &&
          "size-11 rounded-full border-none bg-transparent p-0 shadow-none [--icon-container-default:20px] hover:bg-transparent active:transform-none! aria-expanded:bg-transparent dark:hover:bg-transparent",
      )}
    >
      {isMobile ? (
        <>
          <span className="relative z-10 flex items-center justify-center">{children}</span>
          <OmniGlassSurface width={44} height={44} borderRadius={22} />
        </>
      ) : (
        children
      )}
    </Button>
  );
});
