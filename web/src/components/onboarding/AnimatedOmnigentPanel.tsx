// The onboarding card + animated pixel panel + centered starfish logo.
//
// Ported from the design handoff, with `motion` replaced by CSS transitions
// (see AnimatedOmnigentPanel.css) so we add no animation-library dependency —
// the card/panel resize between onboarding steps via width/height transitions.
// PixelBlast (the WebGL2 shader) has no animation-lib dependency and is used as-is.
//
// The onboarding flow owns the current step and passes the per-step dimensions;
// changing width/height/panelHeight animates to the next value.

import type { ReactNode } from "react";
import PixelBlast from "./PixelBlast";
import omnigentLogo from "@/assets/omnigent-starfish-icon.png";
import "./AnimatedOmnigentPanel.css";

// Fixed card width + logo size — the flow only varies height/panelHeight per
// step, so these never needed to be props.
const CARD_WIDTH = 440;
const LOGO_SIZE = 56;

export interface AnimatedOmnigentPanelProps {
  /** Fixed outer card height in px. Ignored when `autoHeight` is set. */
  height?: number;
  /** Animated pixel panel (canvas) height in px. */
  panelHeight?: number;
  /** Size the card to its content instead of `height` (panel stays fixed). */
  autoHeight?: boolean;
  /** Center the logo in the panel instead of pinning it near the top. */
  centeredLogo?: boolean;
  /** Card body rendered below the panel (the current onboarding step). */
  children?: ReactNode;
}

export function AnimatedOmnigentPanel({
  height = 560,
  panelHeight = 308,
  autoHeight = false,
  centeredLogo = false,
  children,
}: AnimatedOmnigentPanelProps) {
  const reduceMotion =
    typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  return (
    <section
      className={`omnigent-card${autoHeight ? " omnigent-card--auto" : ""}`}
      style={{ width: CARD_WIDTH, ...(autoHeight ? {} : { height }) }}
      aria-label="Omnigent onboarding"
    >
      <div className="omnigent-animated-panel" style={{ height: panelHeight }}>
        <div className="omnigent-pixel-field" aria-hidden="true">
          {/* pixelSize/patternScale/patternDensity match PixelBlast's defaults;
              only speed varies (reduced-motion). */}
          <PixelBlast speed={reduceMotion ? 0 : 0.5} />
        </div>

        <img
          src={omnigentLogo}
          alt="Omnigent"
          className={`omnigent-panel-logo${centeredLogo ? " omnigent-panel-logo--centered" : ""}`}
          style={{ width: LOGO_SIZE, height: LOGO_SIZE }}
        />
      </div>

      {children != null && <div className="omnigent-card-body">{children}</div>}
    </section>
  );
}

export default AnimatedOmnigentPanel;
