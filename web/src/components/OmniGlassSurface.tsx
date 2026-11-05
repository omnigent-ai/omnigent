import GlassSurface from "@/components/ui/glass-surface/GlassSurface";
import { cn } from "@/lib/utils";

interface OmniGlassSurfaceProps {
  width?: number | string;
  height?: number | string;
  borderRadius?: number;
  className?: string;
}

const GLASS_BLUR = 6;
const GLASS_BACKDROP_FILTER = `blur(${GLASS_BLUR}px) saturate(calc(var(--glass-saturation, 1) * 1.4)) brightness(1.04)`;

/** Decorative glass background for a positioned container; content stays in its own layout. */
export function OmniGlassSurface({
  width = "100%",
  height = "100%",
  borderRadius = 16,
  className,
}: OmniGlassSurfaceProps) {
  return (
    <span
      aria-hidden="true"
      className={cn(
        "pointer-events-none absolute inset-0 [color-scheme:light] dark:[color-scheme:dark]",
        className,
      )}
    >
      <GlassSurface
        width={width}
        height={height}
        borderRadius={borderRadius}
        borderWidth={0.06}
        blur={GLASS_BLUR}
        displace={4.4}
        distortionScale={-270}
        refraction={false}
        opacity={0.45}
        className="[&>.glass-surface\_\_sheen]:opacity-0 dark:[&>.glass-surface\_\_rim]:opacity-25 dark:[&>.glass-surface\_\_rim]:p-px!"
        style={{
          background: "hsl(0 0% 100% / 0%)",
          WebkitBackdropFilter: GLASS_BACKDROP_FILTER,
          backdropFilter: GLASS_BACKDROP_FILTER,
          boxShadow: [
            "inset 0 1px 0 0 light-dark(hsl(0 0% 100% / 0.85), hsl(0 0% 100% / 0.15))",
            "inset 0 -1px 0 0 light-dark(hsl(0 0% 100% / 0.35), hsl(0 0% 100% / 0.04))",
            "inset 0 0 2px 1px rgba(212, 212, 216, 0.08)",
            "inset 0 0 12px 3px light-dark(color-mix(in oklch, white, transparent 72%), color-mix(in oklch, white, transparent 95%))",
            "0 2px 6px 1px rgba(17, 17, 26, 0.08)",
          ].join(", "),
        }}
      />
    </span>
  );
}
