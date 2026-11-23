// Draw's marks: their colors, and drawing one onto a canvas. Shared by the live canvas and
// the screenshot, so what's sent is drawn exactly as it was shown.
import type { Shape } from "./designToolsStore";

const BLACK = "#000000";
const WHITE = "#ffffff";

// Drawn into the screenshot, so they're fixed rather than themed.
export const MARK_COLORS = [
  { name: "Red", value: "#e5484d" },
  { name: "Orange", value: "#f76b15" },
  { name: "Yellow", value: "#ffc53d" },
  { name: "Green", value: "#30a46c" },
  { name: "Blue", value: "#3b8ff5" },
  { name: "Black", value: BLACK },
  { name: "White", value: WHITE },
] as const;
const TEXT_FONT = "600 18px system-ui, sans-serif";

export function drawShape(context: CanvasRenderingContext2D, shape: Shape): void {
  const [start, ...rest] = shape.points;
  const end = rest.at(-1) ?? start;
  if (!start) return;
  if (shape.kind === "text") {
    // A contrasting halo keeps the words readable over any part of the page.
    context.save();
    context.font = TEXT_FONT;
    context.textBaseline = "middle";
    context.lineWidth = 4;
    context.strokeStyle = shape.color === WHITE ? BLACK : WHITE;
    context.strokeText(shape.text ?? "", start.x, start.y);
    context.fillStyle = shape.color;
    context.fillText(shape.text ?? "", start.x, start.y);
    context.restore();
    return;
  }
  context.strokeStyle = shape.color;
  context.beginPath();
  if (shape.kind === "pen") {
    context.moveTo(start.x, start.y);
    for (const point of rest) context.lineTo(point.x, point.y);
  } else if (shape.kind === "rect") {
    context.rect(start.x, start.y, end.x - start.x, end.y - start.y);
  } else {
    const angle = Math.atan2(end.y - start.y, end.x - start.x);
    context.moveTo(start.x, start.y);
    context.lineTo(end.x, end.y);
    for (const side of [-1, 1]) {
      context.moveTo(end.x, end.y);
      context.lineTo(
        end.x - 14 * Math.cos(angle + side * 0.5),
        end.y - 14 * Math.sin(angle + side * 0.5),
      );
    }
  }
  context.stroke();
}
