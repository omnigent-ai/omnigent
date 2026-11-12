import type { CSSProperties, ReactElement, ReactNode, RefObject } from "react";

type GlassSurfaceBackdrop = HTMLImageElement | HTMLVideoElement | HTMLCanvasElement;

export interface GlassSurfaceProps {
  children?: ReactNode;
  width?: number | string;
  height?: number | string;
  borderRadius?: number;
  borderWidth?: number;
  brightness?: number;
  opacity?: number;
  blur?: number;
  displace?: number;
  backgroundOpacity?: number;
  saturation?: number;
  distortionScale?: number;
  redOffset?: number;
  greenOffset?: number;
  blueOffset?: number;
  xChannel?: "R" | "G" | "B";
  yChannel?: "R" | "G" | "B";
  mixBlendMode?: CSSProperties["mixBlendMode"];
  refraction?: boolean;
  renderer?: "auto" | "svg" | "webgl";
  backdrop?: RefObject<GlassSurfaceBackdrop | null> | GlassSurfaceBackdrop | null;
  shape?: string;
  className?: string;
  style?: CSSProperties;
}

export default function GlassSurface(props: GlassSurfaceProps): ReactElement;
