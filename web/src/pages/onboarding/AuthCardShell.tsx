// Card shell for the v2 auth pages: the onboarding AnimatedOmnigentPanel with
// no drag strip. Desktop shows the animated card; mobile (<md) drops the chrome
// and animation and lets the content flow.

import type { CSSProperties, ReactNode } from "react";
import { AnimatedOmnigentPanel } from "@/components/onboarding/AnimatedOmnigentPanel";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import omnigentLogo from "@/assets/omnigent-starfish-icon.png";

const DEFAULT_PANEL_HEIGHT = 220;
const MIN_PANEL_HEIGHT = 200;

export function AuthCardShell({
  panelHeight = DEFAULT_PANEL_HEIGHT,
  children,
}: {
  panelHeight?: number;
  children: ReactNode;
}) {
  const isMobile = useIsMobileViewport();

  if (isMobile) {
    // Base padding folded into the safe-area inset (an inline padding would
    // override a Tailwind py-* class).
    const padding = {
      paddingTop: "calc(var(--omnigent-safe-top) + 3rem)",
      paddingBottom: "calc(var(--omnigent-safe-bottom) + 3rem)",
    } as CSSProperties;
    return (
      <div className="flex min-h-screen flex-col items-center bg-background px-4" style={padding}>
        <div className="flex w-full max-w-sm flex-1 flex-col justify-center gap-6">
          <img
            src={omnigentLogo}
            alt="Omnigent"
            className="mx-auto size-14 object-contain"
            aria-hidden="true"
          />
          {children}
        </div>
      </div>
    );
  }

  const safeArea = {
    paddingTop: "var(--omnigent-safe-top)",
    paddingBottom: "var(--omnigent-safe-bottom)",
  } as CSSProperties;
  // my-auto (not items-center) so a card taller than the viewport scrolls from
  // the top instead of being clipped.
  return (
    <div
      className="flex min-h-screen justify-center overflow-y-auto bg-background p-6 [&>*]:my-auto"
      style={safeArea}
    >
      <AnimatedOmnigentPanel
        panelHeight={Math.max(panelHeight, MIN_PANEL_HEIGHT)}
        autoHeight
        centeredLogo
      >
        <div className="flex flex-col px-2 pb-2 pt-3">{children}</div>
      </AnimatedOmnigentPanel>
    </div>
  );
}

export default AuthCardShell;
