interface Rgb {
  r: number;
  g: number;
  b: number;
}

export interface CssColor extends Rgb {
  alpha: number;
}

export function parseCssColor(value: string): CssColor | null {
  const hex = /^#([0-9a-f]{6})([0-9a-f]{2})?$/i.exec(value);
  if (hex) {
    return {
      r: Number.parseInt(hex[1].slice(0, 2), 16),
      g: Number.parseInt(hex[1].slice(2, 4), 16),
      b: Number.parseInt(hex[1].slice(4, 6), 16),
      alpha: hex[2] ? Number.parseInt(hex[2], 16) / 255 : 1,
    };
  }
  const rgb = /^rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)(?:\s*,\s*([\d.]+))?\s*\)$/i.exec(
    value,
  );
  if (!rgb) return null;
  return {
    r: Number(rgb[1]),
    g: Number(rgb[2]),
    b: Number(rgb[3]),
    alpha: rgb[4] ? Number(rgb[4]) : 1,
  };
}

function parseSurface(value: string): CssColor {
  const rgb = parseCssColor(value);
  if (rgb) return rgb;
  const oklch = /^oklch\(\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)\s*\)$/i.exec(value);
  if (!oklch) throw new Error(`Unsupported selection surface: ${value}`);
  const lightness = Number(oklch[1]);
  const chroma = Number(oklch[2]);
  const hue = (Number(oklch[3]) * Math.PI) / 180;
  const a = chroma * Math.cos(hue);
  const b = chroma * Math.sin(hue);
  const l = (lightness + 0.3963377774 * a + 0.2158037573 * b) ** 3;
  const m = (lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3;
  const s = (lightness - 0.0894841775 * a - 1.291485548 * b) ** 3;
  const srgb = (channel: number) =>
    Math.min(
      1,
      Math.max(0, channel <= 0.0031308 ? 12.92 * channel : 1.055 * channel ** (1 / 2.4) - 0.055),
    ) * 255;
  return {
    r: srgb(4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s),
    g: srgb(-1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s),
    b: srgb(-0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s),
    alpha: 1,
  };
}

function luminance(color: Rgb): number {
  const linear = (channel: number) => {
    const value = channel / 255;
    return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * linear(color.r) + 0.7152 * linear(color.g) + 0.0722 * linear(color.b);
}

function contrast(first: number, second: number): number {
  return (Math.max(first, second) + 0.05) / (Math.min(first, second) + 0.05);
}

function toHex(color: Rgb): string {
  return `#${[color.r, color.g, color.b].map((channel) => Math.round(channel).toString(16).padStart(2, "0")).join("")}`;
}

function composite(color: CssColor, background: Rgb): CssColor {
  return {
    r: color.r * color.alpha + background.r * (1 - color.alpha),
    g: color.g * color.alpha + background.g * (1 - color.alpha),
    b: color.b * color.alpha + background.b * (1 - color.alpha),
    alpha: 1,
  };
}

function canvasSurfaces(shellBackground: string, background: CssColor): CssColor[] {
  if (shellBackground === "var(--background)") return [background];
  const stops = [
    ...shellBackground.matchAll(/#[0-9a-f]{8}\b|#[0-9a-f]{6}\b|rgba?\([^)]*\)|oklch\([^)]*\)/gi),
  ].map(([value]) => parseSurface(value));
  let canvases = stops.filter((color) => color.alpha === 1);
  if (!canvases.length) canvases = [background];
  // Sample every solid stop with all combinations of the gradient overlays.
  for (const overlay of stops.filter((color) => color.alpha < 1).reverse()) {
    canvases = [...canvases, ...canvases.map((canvas) => composite(overlay, canvas))];
  }
  return canvases;
}

/** Surfaces are rendered over the first, opaque background. */
export function selectionColors(
  accent: string,
  surfaces: readonly string[],
  shellBackground?: string,
): {
  selectionBackground: string;
  selectionForeground: string;
} {
  const start = parseCssColor(accent);
  if (!start || start.alpha !== 1 || !surfaces.length) {
    throw new Error("Selection colors require an opaque accent and at least one surface");
  }
  const background = parseSurface(surfaces[0]);
  if (background.alpha !== 1) throw new Error("Selection background must be opaque");
  const renderedSurfaces = surfaces.map((surface) => composite(parseSurface(surface), background));
  if (shellBackground) renderedSurfaces.push(...canvasSurfaces(shellBackground, background));
  const surfaceLuminances = renderedSurfaces.map(luminance);
  const score = (color: Rgb) => {
    const value = luminance(color);
    return Math.min(...surfaceLuminances.map((surface) => contrast(value, surface)));
  };
  let selected: Rgb = start;
  let bestScore = score(start);
  let passingWeight = bestScore >= 3 ? 0 : Infinity;

  // Enumerate rounded sRGB colors along both mixes, including narrow passing ranges.
  for (const target of [0, 255]) {
    if (passingWeight === 0) break;
    const changes = new Set<number>([0, 1]);
    for (const channel of [start.r, start.g, start.b]) {
      for (
        let boundary = Math.min(channel, target) + 0.5;
        boundary < Math.max(channel, target);
        boundary += 1
      ) {
        changes.add((boundary - channel) / (target - channel));
      }
    }
    const weights = [...changes].sort((first, second) => first - second);
    for (let index = 1; index < weights.length; index += 1) {
      const weight = (weights[index - 1] + weights[index]) / 2;
      const candidate = {
        r: Math.round(start.r + (target - start.r) * weight),
        g: Math.round(start.g + (target - start.g) * weight),
        b: Math.round(start.b + (target - start.b) * weight),
      };
      const candidateScore = score(candidate);
      if (candidateScore >= 3 && weights[index - 1] < passingWeight) {
        selected = candidate;
        passingWeight = weights[index - 1];
      } else if (passingWeight === Infinity && candidateScore > bestScore) {
        selected = candidate;
        bestScore = candidateScore;
      }
    }
  }

  // Widely separated surfaces can make 3:1 impossible; retain the best minimum ratio.
  const value = luminance(selected);
  return {
    selectionBackground: passingWeight === 0 ? accent : toHex(selected),
    selectionForeground: contrast(value, 0) >= contrast(value, 1) ? "#000000" : "#ffffff",
  };
}
