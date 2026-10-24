import { useCallback, useEffect, useMemo, useState } from "react";
import {
  type Placement,
  compareRooms,
  computePlacement,
  isBasementEligible,
  placementsEqual,
  prunePlacement,
} from "../shared/ranking.ts";
import type { BuildingSnapshot, RoomSettingsPatch } from "../shared/types.ts";
import { type RuntimeOptions, officeApi } from "./api.ts";
import { BuildingView } from "./building/BuildingView.tsx";
import { NewRoomDialog } from "./building/NewRoomDialog.tsx";
import { RuntimeOptionsContext } from "./capabilities.ts";
import { RoomView } from "./room/RoomView.tsx";

const POLL_MS = 5_000;

export function App() {
  const [snapshot, setSnapshot] = useState<BuildingSnapshot | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [displayed, setDisplayed] = useState<Placement | null>(null);
  const [openRoomId, setOpenRoomId] = useState<string | null>(null);
  const [lastRoomId, setLastRoomId] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [creatingRoom, setCreatingRoom] = useState(false);
  const [runtimeOptions, setRuntimeOptions] = useState<RuntimeOptions | null>(null);

  // Refetched when a room or the new-room dialog opens, so newly connected hosts appear.
  useEffect(() => {
    officeApi.runtimeOptions().then(setRuntimeOptions).catch(() => undefined);
  }, [creatingRoom, openRoomId]);

  const latest = useMemo(
    () => (snapshot ? computePlacement(snapshot.rooms, snapshot.serverNow) : null),
    [snapshot],
  );
  const rooms = useMemo(() => new Map((snapshot?.rooms ?? []).map((room) => [room.projectId, room])), [snapshot]);

  // Placement applies only at defined moments; between them rooms stay put.
  const applyPlacement = useCallback((next: BuildingSnapshot) => {
    setDisplayed(computePlacement(next.rooms, next.serverNow));
  }, []);

  const loadSnapshot = useCallback(async () => {
    try {
      const next = await officeApi.building();
      setSnapshot(next);
      setLoadError(null);
      setDisplayed((current) => current ?? computePlacement(next.rooms, next.serverNow));
    } catch (error) {
      setLoadError((error as Error).message);
    }
  }, []);

  useEffect(() => {
    void loadSnapshot();
    const timer = window.setInterval(() => void loadSnapshot(), POLL_MS);
    return () => window.clearInterval(timer);
  }, [loadSnapshot]);

  const shown = displayed && snapshot ? prunePlacement(displayed, new Set(rooms.keys())) : null;
  const placementWaiting = Boolean(shown && latest && !placementsEqual(shown, latest));

  const refreshPlacement = async () => {
    const next = await officeApi.refreshBuilding();
    setSnapshot(next);
    applyPlacement(next);
  };

  const updateRoom = async (projectId: string, patch: RoomSettingsPatch) => {
    const next = await officeApi.updateRoom(projectId, patch);
    setSnapshot(next);
    applyPlacement(next);
  };

  const exitRoom = useCallback(() => {
    setLastRoomId(openRoomId);
    setOpenRoomId(null);
    if (snapshot) applyPlacement(snapshot);
  }, [openRoomId, snapshot, applyPlacement]);

  const openRoom = openRoomId ? rooms.get(openRoomId) : undefined;
  if (openRoom && snapshot) {
    return (
      <RuntimeOptionsContext.Provider value={runtimeOptions}>
      <RoomView
        room={openRoom}
        allRooms={snapshot.rooms}
        serverNow={snapshot.serverNow}
        placementWaiting={placementWaiting}
        onExit={exitRoom}
        onUpdateRoom={(patch) => updateRoom(openRoom.projectId, patch)}
      />
      </RuntimeOptionsContext.Provider>
    );
  }

  const sortedRooms = [...(snapshot?.rooms ?? [])].sort(compareRooms);
  const query = search.trim().toLowerCase();
  const matches = query ? sortedRooms.filter((room) => room.name.toLowerCase().includes(query)) : [];
  const attention = sortedRooms.filter((room) => room.pendingRequestCount > 0);

  return (
    <div className="app">
      <header className="toolbar">
        <h1>Omnigent Office</h1>
        <span className={`connection ${snapshot?.connection.ok ? "ok" : "down"}`} role="status">
          {loadError
            ? `Office service unreachable: ${loadError}`
            : snapshot
              ? snapshot.connection.ok
                ? `Connected to ${snapshot.connection.omnigentUrl}`
                : `Omnigent unreachable (${snapshot.connection.omnigentUrl}): ${snapshot.connection.error}`
              : "Connecting…"}
        </span>
        {placementWaiting ? <span className="placement-waiting">Placement updates available</span> : null}
        <button type="button" onClick={() => void refreshPlacement()}>
          Refresh placement
        </button>
      </header>
      {snapshot?.connection.ok && snapshot.rooms.length === 0 ? (
        <p className="empty-building" role="note">
          This Omnigent instance has no projects yet, so the building has no rooms. Use “New room” in the lobby, or
          create a project in the Omnigent web UI; each project becomes a room here.
        </p>
      ) : null}
      <div className="building-layout">
        {shown ? (
          <BuildingView
            placement={shown}
            rooms={rooms}
            onEnterRoom={setOpenRoomId}
            onNewRoom={() => setCreatingRoom(true)}
            elevatorProjectId={lastRoomId}
          />
        ) : (
          <p className="muted">Loading building…</p>
        )}
        <aside className="side-panel">
          <section aria-label="Attention">
            <h2>Needs your attention</h2>
            {attention.length === 0 ? <p className="muted">Nothing is waiting on you.</p> : null}
            <ul className="room-list">
              {attention.map((room) => (
                <li key={room.projectId}>
                  <button type="button" onClick={() => setOpenRoomId(room.projectId)}>
                    ⚑ {room.name} — {room.pendingRequestCount} request{room.pendingRequestCount === 1 ? "" : "s"}
                    {snapshot && isBasementEligible(room, snapshot.serverNow) ? " (basement)" : ""}
                  </button>
                </li>
              ))}
            </ul>
          </section>
          <section aria-label="Find a project">
            <h2>
              <label htmlFor="project-search">Find a project</label>
            </h2>
            <input
              id="project-search"
              type="search"
              placeholder="Project name"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
            <ul className="room-list">
              {(query ? matches : sortedRooms).map((room) => (
                <li key={room.projectId}>
                  <button type="button" onClick={() => setOpenRoomId(room.projectId)}>
                    {room.icon ? `${room.icon} ` : ""}
                    {room.name}
                    <span className="muted small">
                      {" "}
                      · {room.priority}
                      {snapshot && isBasementEligible(room, snapshot.serverNow) ? " · basement" : ""}
                    </span>
                  </button>
                </li>
              ))}
              {query && matches.length === 0 ? <li className="muted">No matching projects.</li> : null}
            </ul>
          </section>
          <p className="muted small">
            Click a room or use Tab and Enter to ride the elevator. Inside a room, walk with arrow keys or WASD.
          </p>
        </aside>
      </div>
      {creatingRoom ? (
        <RuntimeOptionsContext.Provider value={runtimeOptions}>
        <NewRoomDialog
          onClose={() => setCreatingRoom(false)}
          onCreated={(next) => {
            setCreatingRoom(false);
            setSnapshot(next);
            applyPlacement(next);
          }}
        />
        </RuntimeOptionsContext.Provider>
      ) : null}
    </div>
  );
}
