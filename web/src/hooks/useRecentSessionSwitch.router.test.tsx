// ⌃] with a real router: releasing Control commits one route, which re-renders
// the sidebar and changes useNavigate's identity, and the next switch starts from it.

import { act, render } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { BrowserRouter, Route, Routes, useLocation, useParams } from "react-router-dom";

import {
  useRecentSessionHotkey,
  useRecentSessionSwitchStore,
  useRecordRecentSessionVisits,
  useRegisterLiveSessions,
} from "./useRecentSessionSwitch";
import { usePublishSidebarSessionOrder } from "./useSidebarSessionOrder";

const rows = ["a", "b", "c"];
const liveRows = new Set(rows);
const readLiveRows = () => liveRows;
let pathname = "";

/** The sidebar's recency wiring on a session route, as Sidebar mounts it. */
function SessionSidebar() {
  const { id } = useParams();
  usePublishSidebarSessionOrder(rows, id ?? null);
  useRegisterLiveSessions(readLiveRows);
  useRecordRecentSessionVisits(id);
  useRecentSessionHotkey(true);
  return null;
}

function LocationProbe() {
  pathname = useLocation().pathname;
  return null;
}

function pressControlBracket(type: "keydown" | "keyup", key = "]") {
  act(() => {
    window.dispatchEvent(
      new KeyboardEvent(type, {
        key: type === "keyup" ? "Control" : key,
        code: type === "keyup" ? "ControlLeft" : "BracketRight",
        ctrlKey: type === "keydown",
        bubbles: true,
        cancelable: true,
      }),
    );
  });
}

afterEach(() => {
  localStorage.clear();
  useRecentSessionSwitchStore.setState({ history: [], switcher: null, chordOwner: null });
  window.history.replaceState(null, "", "/");
});

it("opens one route on release, and the next switch starts from it", () => {
  useRecentSessionSwitchStore.setState({ history: ["a", "b", "c"], switcher: null });
  window.history.replaceState(null, "", "/c/a");
  render(
    <BrowserRouter>
      <LocationProbe />
      <Routes>
        <Route path="/c/:id" element={<SessionSidebar />} />
      </Routes>
    </BrowserRouter>,
  );

  pressControlBracket("keydown");
  pressControlBracket("keydown");
  // Cycling doesn't leave the open session; the release does.
  expect(pathname).toBe("/c/a");
  pressControlBracket("keyup");
  expect(pathname).toBe("/c/c");
  expect(useRecentSessionSwitchStore.getState().history).toEqual(["c", "a", "b"]);

  pressControlBracket("keydown");
  pressControlBracket("keyup");
  expect(pathname).toBe("/c/a");
});
