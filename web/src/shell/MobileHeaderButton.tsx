import { createContext, forwardRef, useContext, type ComponentPropsWithoutRef } from "react";
import { OmniGlassSurface } from "@/components/OmniGlassSurface";
import { Button } from "@/components/ui/button";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { cn } from "@/lib/utils";

const MobileHeaderButtonGroupContext = createContext(false);

export function MobileHeaderButtonGroup({
  children,
  className,
  ...props
}: ComponentPropsWithoutRef<"div">) {
  const isMobile = useIsMobileViewport();
  return (
    <MobileHeaderButtonGroupContext.Provider value={isMobile}>
      <div
        {...props}
        data-mobile-header-group=""
        className={cn(
          className,
          isMobile && "relative isolate h-11 rounded-[20px] [&:not(:has(button))]:hidden",
        )}
      >
        {isMobile && <OmniGlassSurface borderRadius={20} className="-z-10" />}
        {children}
      </div>
    </MobileHeaderButtonGroupContext.Provider>
  );
}

/** Keep the native button as the Radix trigger and layer glass behind its icon. */
export const MobileHeaderButton = forwardRef<
  HTMLButtonElement,
  Omit<ComponentPropsWithoutRef<typeof Button>, "asChild"> & { glass?: boolean }
>(function MobileHeaderButton({ children, className, size, variant, glass = true, ...props }, ref) {
  const isMobile = useIsMobileViewport();
  const grouped = useContext(MobileHeaderButtonGroupContext);
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
          {glass && !grouped && <OmniGlassSurface width={44} height={44} borderRadius={22} />}
        </>
      ) : (
        children
      )}
    </Button>
  );
});
