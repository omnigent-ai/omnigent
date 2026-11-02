// Procedural pixel-art sprites drawn on a 2D canvas, so the prototype needs
// no image assets or renderer dependency.

import { type WorkerState, appearanceIndex } from "./workers.ts";

export interface Palette {
  skin: string;
  hair: string;
  shirt: string;
  pants: string;
}

export const WORKER_PALETTES: Palette[] = [
  { skin: "#f1c27d", hair: "#3b2a1a", shirt: "#4f7cac", pants: "#2d3142" },
  { skin: "#c68642", hair: "#111111", shirt: "#7a9e4f", pants: "#3a3a3a" },
  { skin: "#ffdbac", hair: "#b55239", shirt: "#a05195", pants: "#26313d" },
  { skin: "#8d5524", hair: "#2b1d0e", shirt: "#d1913c", pants: "#2f2f46" },
  { skin: "#e0ac69", hair: "#6b4e2e", shirt: "#3aa6a0", pants: "#36454f" },
];

/** A worker's colors, stable for the same agent name or session id. */
export function workerPalette(key: string): Palette {
  return WORKER_PALETTES[appearanceIndex(key, WORKER_PALETTES.length)];
}

export const HUMAN_PALETTE: Palette = { skin: "#ffe0bd", hair: "#5a3825", shirt: "#e4572e", pants: "#1d3557" };

// 8x12 person template: s=skin h=hair t=shirt p=pants e=eye.
const PERSON = [
  "..hhhh..",
  ".hhhhhh.",
  ".hsssss.",
  ".sesses.",
  ".ssssss.",
  "..ssss..",
  ".tttttt.",
  "tttttttt",
  "s.tttt.s",
  "..pppp..",
  "..p..p..",
  "..p..p..",
];

export function drawPerson(
  context: CanvasRenderingContext2D,
  x: number,
  y: number,
  pixel: number,
  palette: Palette,
  stepFrame = 0,
): void {
  const colors: Record<string, string> = {
    s: palette.skin,
    h: palette.hair,
    t: palette.shirt,
    p: palette.pants,
    e: "#1b1b1b",
  };
  PERSON.forEach((row, rowIndex) => {
    // Alternate the legs while walking.
    const line = rowIndex >= 10 && stepFrame % 2 === 1 ? (rowIndex === 10 ? ".p....p." : "p......p") : row;
    [...line].forEach((cell, columnIndex) => {
      const color = colors[cell];
      if (!color) return;
      context.fillStyle = color;
      context.fillRect(x + columnIndex * pixel, y + rowIndex * pixel, pixel, pixel);
    });
  });
}

export const PERSON_WIDTH = 8;
export const PERSON_HEIGHT = 12;

const STATE_COLORS: Record<WorkerState, string> = {
  working: "#3fa34d",
  waiting_human: "#f2c14e",
  waiting_dependency: "#5c9ead",
  needs_help: "#ff7a45",
  failed: "#d7263d",
  offline: "#8d8d8d",
  idle: "#c6e8b0",
};

const STATE_GLYPHS: Record<WorkerState, string> = {
  working: "…",
  waiting_human: "!",
  waiting_dependency: "⧗",
  needs_help: "?",
  failed: "×",
  offline: "z",
  idle: "–",
};

/** A small speech-bubble badge above a worker showing its state. */
export function drawStateBadge(context: CanvasRenderingContext2D, centerX: number, y: number, state: WorkerState): void {
  const size = 14;
  context.fillStyle = STATE_COLORS[state];
  context.fillRect(centerX - size / 2, y - size, size, size);
  context.fillStyle = "#111";
  context.font = "bold 11px monospace";
  context.textAlign = "center";
  context.textBaseline = "middle";
  context.fillText(STATE_GLYPHS[state], centerX, y - size / 2 + 1);
}

export function drawDesk(context: CanvasRenderingContext2D, x: number, y: number, width: number, height: number, lit: boolean): void {
  context.fillStyle = "#6b4226";
  context.fillRect(x, y, width, height);
  context.fillStyle = "#8b5a2b";
  context.fillRect(x + 2, y + 2, width - 4, height - 6);
  // Monitor
  context.fillStyle = "#222";
  context.fillRect(x + width / 2 - 9, y - 12, 18, 12);
  context.fillStyle = lit ? "#7fd1ff" : "#38424b";
  context.fillRect(x + width / 2 - 7, y - 10, 14, 8);
}

export function drawFloorTiles(context: CanvasRenderingContext2D, width: number, height: number, tile: number): void {
  for (let row = 0; row * tile < height; row++) {
    for (let column = 0; column * tile < width; column++) {
      context.fillStyle = (row + column) % 2 === 0 ? "#d9c7a7" : "#cfbb98";
      context.fillRect(column * tile, row * tile, tile, tile);
    }
  }
}
