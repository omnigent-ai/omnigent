// The desktop app's screenshots for the design tools, from main's captures of the page
// alone: Annotate's of the element it picked, and Draw's with the marks drawn on.
import type { CssRect, Shape } from "./core/designToolsStore";
import { drawShape } from "./core/marks";
import type { DesignOverlay, PageCapture } from "./desktopOverlay";
import { prepareMarksCanvas } from "./layerParts";

function pngFile(dataUrl: string, name: string): File {
  const base64 = dataUrl.slice(dataUrl.indexOf(",") + 1);
  const bytes = Uint8Array.from(atob(base64), (char) => char.charCodeAt(0));
  return new File([bytes], name, { type: "image/png" });
}

/** A capture of the page round a picked element, a little larger than it. */
export async function elementScreenshot(overlay: DesignOverlay, element: CssRect): Promise<File> {
  const pad = 8;
  const x = Math.max(0, element.x - pad);
  const y = Math.max(0, element.y - pad);
  // Its right and bottom edges are main's to keep inside the viewport as it is then.
  const right = element.x + element.width + pad;
  const bottom = element.y + element.height + pad;
  if (right - x < 2 || bottom - y < 2) throw new Error("The element isn't on screen.");
  const shot = await overlay.capture({ x, y, width: right - x, height: bottom - y });
  return pngFile(shot.png, "design-element.png");
}

async function loadImage(source: string): Promise<HTMLImageElement> {
  const image = new Image();
  image.src = source;
  await image.decode();
  return image;
}

/**
 * The page's capture with Draw's marks on it, as Draw's screenshot. The marks are in the
 * page's CSS pixels, so they go on the capture at its own viewport.
 */
export async function drawingScreenshot(
  shot: PageCapture,
  shapes: readonly Shape[],
): Promise<File> {
  const { viewport } = shot;
  if (!(viewport.width > 0 && viewport.height > 0))
    throw new Error("Couldn't draw the screenshot.");
  const image = await loadImage(shot.png);
  const canvas = document.createElement("canvas");
  canvas.width = image.naturalWidth;
  canvas.height = image.naturalHeight;
  const context = canvas.getContext("2d");
  if (!context) throw new Error("Couldn't draw the screenshot.");
  prepareMarksCanvas(context, viewport.width, viewport.height, canvas.width / viewport.width);
  context.drawImage(image, 0, 0, viewport.width, viewport.height);
  for (const shape of shapes) drawShape(context, shape);
  const blob = await new Promise<Blob | null>((resolve) => {
    canvas.toBlob(resolve, "image/png");
  });
  if (!blob) throw new Error("Couldn't draw the screenshot.");
  return new File([blob], "design-drawing.png", { type: "image/png" });
}
