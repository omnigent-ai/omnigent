import { type MouseEvent, useCallback, useEffect, useRef, useState } from "react";
import type { WorkerSession } from "../api.ts";
import { HUMAN_PALETTE, PERSON_WIDTH, drawDesk, drawFloorTiles, drawPerson, drawStateBadge, workerPalette } from "../sprites.ts";
import type { WorkerState } from "../workers.ts";
import {
  DESK_WIDTH,
  TILE,
  type Desk,
  type RoomLayout,
  type Tile,
  deskAtTile,
  deskInReach,
  findPath,
  isWalkable,
  pixelToTile,
  standingTile,
} from "./roomLayout.ts";

const STEP_MS = 110;
const PIXEL = 3;

const MOVES: Record<string, [number, number]> = {
  ArrowUp: [0, -1],
  ArrowDown: [0, 1],
  ArrowLeft: [-1, 0],
  ArrowRight: [1, 0],
  w: [0, -1],
  s: [0, 1],
  a: [-1, 0],
  d: [1, 0],
};

interface RoomCanvasProps {
  layout: RoomLayout;
  workers: WorkerSession[];
  /** Each worker's room state, shared with the worker list and the Team tab. */
  stateOf: (worker: WorkerSession) => WorkerState;
  selectedWorkerId: string | null;
  onOpenWorker: (sessionId: string) => void;
  onExit: () => void;
  /** Suspend keyboard handling while a dialog owns focus. */
  inputEnabled: boolean;
}

export function RoomCanvas({ layout, workers, stateOf, selectedWorkerId, onOpenWorker, onExit, inputEnabled }: RoomCanvasProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const avatar = useRef<{ position: Tile; path: Tile[]; onArrive: (() => void) | null; steps: number }>({
    position: layout.spawn,
    path: [],
    onArrive: null,
    steps: 0,
  });
  const workersRef = useRef(workers);
  workersRef.current = workers;
  const stateOfRef = useRef(stateOf);
  stateOfRef.current = stateOf;

  const width = layout.columns * TILE;
  const height = layout.rows * TILE;
  // Fill the space around the canvas at the room's proportions, and draw at a whole
  // multiple of the room's size for that space and the screen density, so art pixels
  // stay square and crisp instead of being stretched.
  const [fit, setFit] = useState<{ shown: number; scale: number } | null>(null);
  const scaleRef = useRef(1);
  scaleRef.current = fit?.scale ?? 1;

  useEffect(() => {
    const container = canvasRef.current?.parentElement;
    if (!container || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(([entry]) => {
      const { width: spaceWidth, height: spaceHeight } = entry.contentRect;
      // A hidden stage (a file tab is open) measures zero; keep the last size.
      if (spaceWidth <= 0 || spaceHeight <= 0) return;
      const shown = Math.min(spaceWidth / width, spaceHeight / height);
      setFit({ shown, scale: Math.max(1, Math.min(8, Math.ceil(shown * (window.devicePixelRatio || 1)))) });
    });
    observer.observe(container);
    return () => observer.disconnect();
  }, [width, height]);
  const scale = fit?.scale ?? 1;

  const interactWith = useCallback(
    (desk: Desk) => {
      const worker = workersRef.current[desk.workerIndex];
      if (worker) onOpenWorker(worker.id);
    },
    [onOpenWorker],
  );

  const walkTo = useCallback(
    (target: Tile, onArrive: (() => void) | null) => {
      const state = avatar.current;
      const atTarget = state.position.column === target.column && state.position.row === target.row;
      state.path = atTarget ? [] : findPath(layout, state.position, target);
      state.onArrive = state.path.length || atTarget ? onArrive : null;
      if (atTarget) {
        state.onArrive = null;
        onArrive?.();
      }
    },
    [layout],
  );

  useEffect(() => {
    let frame = 0;
    let lastStep = performance.now();
    const render = (now: number) => {
      const state = avatar.current;
      if (state.path.length && now - lastStep >= STEP_MS) {
        state.position = state.path.shift() as Tile;
        state.steps += 1;
        lastStep = now;
        if (!state.path.length && state.onArrive) {
          const arrive = state.onArrive;
          state.onArrive = null;
          arrive();
        }
      }
      const context = canvasRef.current?.getContext("2d");
      if (context) {
        context.setTransform(scaleRef.current, 0, 0, scaleRef.current, 0, 0);
        drawFloorTiles(context, width, height, TILE);
        context.fillStyle = "#7a8b99";
        context.fillRect(0, 0, width, TILE * 2);
        context.fillStyle = "#5d6b77";
        context.fillRect(0, 0, TILE, height);
        context.fillRect(width - TILE, 0, TILE, height);
        context.fillRect(0, height - TILE, width, TILE);
        // Windows on the back wall.
        for (let column = 2; column < layout.columns - 2; column += 4) {
          context.fillStyle = "#bfe3f2";
          context.fillRect(column * TILE, 14, TILE * 2, TILE);
        }
        // Door back to the elevator.
        context.fillStyle = "#c9a227";
        context.fillRect(layout.door.column * TILE, layout.door.row * TILE, TILE, TILE);
        context.fillStyle = "#1f1f1f";
        context.font = "10px monospace";
        context.textAlign = "center";
        context.fillText("EXIT", layout.door.column * TILE + TILE / 2, layout.door.row * TILE + TILE / 2 + 4);

        layout.desks.forEach((desk) => {
          const worker = workersRef.current[desk.workerIndex];
          if (!worker) return;
          const workerState = stateOfRef.current(worker);
          const palette = workerPalette(worker.agent_name ?? worker.id);
          const seatX = desk.column * TILE + (DESK_WIDTH * TILE - PERSON_WIDTH * PIXEL) / 2;
          const seatY = (desk.row - 1) * TILE - 4;
          drawPerson(context, seatX, seatY, PIXEL, palette, workerState === "working" ? Math.floor(now / 350) : 0);
          drawDesk(context, desk.column * TILE + 2, desk.row * TILE + 4, DESK_WIDTH * TILE - 4, TILE - 6, workerState === "working");
          drawStateBadge(context, desk.column * TILE + TILE, seatY - 2, workerState);
          if (worker.id === selectedWorkerId) {
            context.strokeStyle = "#e4572e";
            context.lineWidth = 2;
            context.strokeRect(desk.column * TILE, (desk.row - 1) * TILE - 18, DESK_WIDTH * TILE, TILE * 2 + 18);
          }
          context.fillStyle = "#1f1f1f";
          context.font = "10px monospace";
          context.textAlign = "center";
          const label = (worker.title ?? worker.agent_name ?? "Worker").slice(0, 12);
          context.fillText(label, desk.column * TILE + TILE, desk.row * TILE + TILE + 2);
        });
        const { position } = state;
        drawPerson(
          context,
          position.column * TILE + (TILE - PERSON_WIDTH * PIXEL) / 2,
          position.row * TILE - 6,
          PIXEL,
          HUMAN_PALETTE,
          state.path.length ? state.steps : 0,
        );
      }
      frame = requestAnimationFrame(render);
    };
    frame = requestAnimationFrame(render);
    return () => cancelAnimationFrame(frame);
  }, [layout, width, height, selectedWorkerId]);

  useEffect(() => {
    if (!inputEnabled) return;
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.tagName === "SELECT")) return;
      const state = avatar.current;
      const move = MOVES[event.key];
      if (move) {
        event.preventDefault();
        const next = { column: state.position.column + move[0], row: state.position.row + move[1] };
        state.path = [];
        state.onArrive = null;
        if (isWalkable(layout, next)) {
          state.position = next;
          state.steps += 1;
        }
        return;
      }
      if (event.key === "Enter" || event.key === "e" || event.key === " ") {
        if (target && target !== document.body && target !== canvasRef.current) return;
        event.preventDefault();
        if (state.position.column === layout.door.column && state.position.row === layout.door.row) {
          onExit();
          return;
        }
        const desk = deskInReach(layout, state.position);
        if (desk) interactWith(desk);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [inputEnabled, layout, interactWith, onExit]);

  const onClick = (event: MouseEvent<HTMLCanvasElement>) => {
    const bounds = event.currentTarget.getBoundingClientRect();
    const tile = pixelToTile(
      ((event.clientX - bounds.left) / bounds.width) * width,
      ((event.clientY - bounds.top) / bounds.height) * height,
    );
    const desk = deskAtTile(layout, tile);
    if (desk) {
      walkTo(standingTile(desk), () => interactWith(desk));
    } else if (tile.column === layout.door.column && tile.row === layout.door.row) {
      walkTo(tile, onExit);
    } else if (isWalkable(layout, tile)) {
      walkTo(tile, null);
    }
  };

  return (
    <canvas
      ref={canvasRef}
      className="room-canvas"
      width={width * scale}
      height={height * scale}
      style={fit ? { width: Math.floor(width * fit.shown), height: Math.floor(height * fit.shown) } : undefined}
      tabIndex={0}
      aria-label="Room interior. Use arrow keys or WASD to walk, Enter at a desk to talk to a worker, Enter on the exit to leave. Every worker is also listed in the side panel."
      onClick={onClick}
    />
  );
}
