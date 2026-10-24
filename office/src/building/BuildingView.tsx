import { useEffect, useMemo, useRef, useState } from "react";
import type { Placement } from "../../shared/ranking.ts";
import type { RoomSummary } from "../../shared/types.ts";
import { HUMAN_PALETTE, WORKER_PALETTES, drawPerson } from "../sprites.ts";
import {
  BUILDING_WIDTH,
  FLOOR_HEIGHT,
  SHAFT_WIDTH,
  SLOT_GAP,
  SLOT_WIDTH,
  type FloorRow,
  type Rect,
  buildingHeight,
  buildingRows,
  rowIndexOfProject,
  rowTop,
  slotPositions,
} from "./buildingLayout.ts";

const MOVE_MS = 700;
const ELEVATOR_MS = 450;

interface BuildingViewProps {
  placement: Placement;
  rooms: Map<string, RoomSummary>;
  onEnterRoom: (projectId: string) => void;
  /** The floor the elevator waits on, e.g. after returning from a room. */
  elevatorProjectId: string | null;
}

function drawRoomPreview(context: CanvasRenderingContext2D, rect: Rect, room: RoomSummary, basement: boolean): void {
  context.fillStyle = basement ? "#4b4b5a" : "#f4ead5";
  context.fillRect(rect.x, rect.y, rect.width, rect.height);
  context.strokeStyle = "#2b2b2b";
  context.lineWidth = 3;
  context.strokeRect(rect.x, rect.y, rect.width, rect.height);
  // Floorboards
  context.fillStyle = basement ? "#3a3a46" : "#d9c7a7";
  context.fillRect(rect.x + 2, rect.y + rect.height - 26, rect.width - 4, 24);
  const visibleWorkers = Math.min(room.workerCount, 5);
  for (let index = 0; index < visibleWorkers; index++) {
    const working = index < room.activeWorkerCount;
    drawPerson(context, rect.x + 14 + index * 34, rect.y + rect.height - 52, 2, WORKER_PALETTES[index % WORKER_PALETTES.length], working ? Math.floor(Date.now() / 300) + index : 0);
  }
  context.fillStyle = basement ? "#e6e6e6" : "#1f1f1f";
  context.font = "bold 13px monospace";
  context.textAlign = "left";
  context.textBaseline = "top";
  const title = `${room.icon ? `${room.icon} ` : ""}${room.name}`;
  context.fillText(title.length > 24 ? `${title.slice(0, 23)}…` : title, rect.x + 8, rect.y + 8);
  context.font = "11px monospace";
  const priorityMark = room.priority === "critical" ? "‼ Critical" : room.priority === "high" ? "↑ High" : "";
  context.fillText(`${room.workerCount} worker${room.workerCount === 1 ? "" : "s"} ${priorityMark}`, rect.x + 8, rect.y + 26);
  if (room.pendingRequestCount > 0) {
    // Attention flag
    const flagX = rect.x + rect.width - 30;
    context.fillStyle = "#2b2b2b";
    context.fillRect(flagX, rect.y + 6, 3, 30);
    context.fillStyle = "#f2c14e";
    context.fillRect(flagX + 3, rect.y + 6, 20, 14);
    context.fillStyle = "#1f1f1f";
    context.font = "bold 12px monospace";
    context.fillText("!", flagX + 9, rect.y + 7);
  }
}

function drawBuilding(
  context: CanvasRenderingContext2D,
  rows: FloorRow[],
  rooms: Map<string, RoomSummary>,
  drawPositions: Map<string, Rect>,
  elevatorY: number,
): void {
  const height = buildingHeight(rows);
  context.clearRect(0, 0, BUILDING_WIDTH, height);
  const lobbyIndex = rows.findIndex((row) => row.kind === "lobby");
  rows.forEach((row, rowIndex) => {
    const top = rowTop(rows, rowIndex);
    context.fillStyle = row.kind === "basement" ? "#2e2e38" : row.kind === "lobby" ? "#b9a37e" : "#8c6e54";
    context.fillRect(0, top, BUILDING_WIDTH, FLOOR_HEIGHT);
    context.fillStyle = "#3d2b1f";
    context.fillRect(0, top + FLOOR_HEIGHT - 6, BUILDING_WIDTH, 6);
  });
  if (rows.some((row) => row.kind === "basement")) {
    // Ground line between the lobby and the basement.
    context.fillStyle = "#4e7d3a";
    context.fillRect(0, rowTop(rows, lobbyIndex) + FLOOR_HEIGHT, BUILDING_WIDTH, 18);
  }
  // Elevator shaft and car.
  context.fillStyle = "#555";
  context.fillRect(8, 0, SHAFT_WIDTH - 16, buildingHeight(rows));
  context.fillStyle = "#c9a227";
  context.fillRect(12, elevatorY + 30, SHAFT_WIDTH - 24, FLOOR_HEIGHT - 42);
  context.fillStyle = "#8a6d12";
  context.fillRect(SHAFT_WIDTH / 2 - 1, elevatorY + 30, 2, FLOOR_HEIGHT - 42);
  drawPerson(context, SHAFT_WIDTH / 2 - 8, elevatorY + FLOOR_HEIGHT - 42, 2, HUMAN_PALETTE);
  // Floor numbers on the shaft wall, above the car.
  rows.forEach((row, rowIndex) => {
    context.fillStyle = "#f6f1e7";
    context.font = "bold 14px monospace";
    context.textAlign = "center";
    context.textBaseline = "top";
    context.fillText(row.label, SHAFT_WIDTH / 2, rowTop(rows, rowIndex) + 8);
  });
  // Lobby: reception desk and the reserved new-room entry.
  const lobbyTop = rowTop(rows, lobbyIndex);
  context.fillStyle = "#6b4226";
  context.fillRect(SHAFT_WIDTH + SLOT_GAP + 20, lobbyTop + FLOOR_HEIGHT - 44, 120, 30);
  context.fillStyle = "#f6f1e7";
  context.font = "12px monospace";
  context.textAlign = "left";
  context.fillText("Reception", SHAFT_WIDTH + SLOT_GAP + 40, lobbyTop + FLOOR_HEIGHT - 36);
  const newRoomX = SHAFT_WIDTH + SLOT_GAP + 2 * (SLOT_WIDTH + SLOT_GAP);
  context.strokeStyle = "#f6f1e7";
  context.setLineDash([6, 4]);
  context.strokeRect(newRoomX, lobbyTop + 14, SLOT_WIDTH, FLOOR_HEIGHT - 22);
  context.setLineDash([]);
  context.fillText("New room — coming soon", newRoomX + 14, lobbyTop + 54);

  rows.forEach((row) =>
    row.projectIds.forEach((projectId) => {
      const room = rooms.get(projectId);
      const rect = drawPositions.get(projectId);
      if (room && rect) drawRoomPreview(context, rect, room, row.kind === "basement");
    }),
  );
}

export function BuildingView({ placement, rooms, onEnterRoom, elevatorProjectId }: BuildingViewProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const rows = useMemo(() => buildingRows(placement), [placement]);
  const targets = useMemo(() => slotPositions(rows), [rows]);
  const previousTargets = useRef<Map<string, Rect>>(targets);
  const moveStartedAt = useRef(0);
  const [entering, setEntering] = useState<string | null>(null);
  const elevator = useRef({ from: 0, to: 0, startedAt: 0 });

  const lobbyIndex = rows.findIndex((row) => row.kind === "lobby");
  const floorTop = (projectId: string | null) => {
    const index = projectId ? rowIndexOfProject(rows, projectId) : -1;
    return rowTop(rows, index >= 0 ? index : lobbyIndex);
  };

  useEffect(() => {
    // Placement changed: animate rooms from where they were drawn.
    moveStartedAt.current = performance.now();
    return () => {
      previousTargets.current = targets;
    };
  }, [targets]);

  useEffect(() => {
    const top = floorTop(elevatorProjectId);
    elevator.current = { from: top, to: top, startedAt: 0 };
    // Only reposition when the waiting floor itself changes.
  }, [elevatorProjectId]);

  useEffect(() => {
    let frame = 0;
    const render = () => {
      const context = canvasRef.current?.getContext("2d");
      if (context) {
        const now = performance.now();
        const progress = Math.min(1, (now - moveStartedAt.current) / MOVE_MS);
        const drawPositions = new Map<string, Rect>();
        targets.forEach((target, projectId) => {
          const start = previousTargets.current.get(projectId) ?? target;
          drawPositions.set(projectId, {
            ...target,
            x: start.x + (target.x - start.x) * progress,
            y: start.y + (target.y - start.y) * progress,
          });
        });
        const ride = elevator.current;
        const rideProgress = ride.startedAt ? Math.min(1, (now - ride.startedAt) / ELEVATOR_MS) : 1;
        drawBuilding(context, rows, rooms, drawPositions, ride.from + (ride.to - ride.from) * rideProgress);
      }
      frame = requestAnimationFrame(render);
    };
    frame = requestAnimationFrame(render);
    return () => cancelAnimationFrame(frame);
  }, [rows, rooms, targets]);

  const enter = (projectId: string) => {
    if (entering) return;
    const ride = elevator.current;
    elevator.current = { from: ride.to, to: floorTop(projectId), startedAt: performance.now() };
    setEntering(projectId);
    window.setTimeout(() => {
      setEntering(null);
      onEnterRoom(projectId);
    }, ELEVATOR_MS + 80);
  };

  const height = buildingHeight(rows);
  return (
    <div className="building-scroll">
      <div className="building" style={{ width: BUILDING_WIDTH, height }}>
        <canvas ref={canvasRef} width={BUILDING_WIDTH} height={height} aria-hidden="true" />
        {rows.map((row, rowIndex) =>
          row.projectIds.map((projectId, slotIndex) => {
            const room = rooms.get(projectId);
            if (!room) return null;
            return (
              <button
                key={projectId}
                type="button"
                className="room-hotspot"
                style={{
                  left: SHAFT_WIDTH + SLOT_GAP + slotIndex * (SLOT_WIDTH + SLOT_GAP),
                  top: rowTop(rows, rowIndex) + 14,
                  width: SLOT_WIDTH,
                  height: FLOOR_HEIGHT - 22,
                }}
                aria-label={`Enter ${room.name}, floor ${row.label}${
                  room.pendingRequestCount === 1
                    ? ", 1 request needs your attention"
                    : room.pendingRequestCount > 1
                      ? `, ${room.pendingRequestCount} requests need your attention`
                      : ""
                }`}
                disabled={entering !== null}
                onClick={() => enter(projectId)}
              />
            );
          }),
        )}
      </div>
    </div>
  );
}
