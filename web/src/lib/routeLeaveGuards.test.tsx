import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { useGoogleDocsEditorLifecycle } from "@/components/GoogleDocsPane/editorLifecycle";
import { Link, useLocation, useNavigate } from "./routing";
import { registerRouteLeaveGuard } from "./routeLeaveGuards";

function EditorRoute() {
  useGoogleDocsEditorLifecycle(true);
  const navigate = useNavigate();
  const location = useLocation();
  return (
    <>
      <iframe title="Retained editor" sandbox="" />
      <span>
        {location.pathname}
        {location.search}
      </span>
      <Link to="/settings">Settings</Link>
      <button type="button" onClick={() => navigate("/c/other")}>
        Other session
      </button>
      <button type="button" onClick={() => navigate("?panel=docs")}>
        Same route
      </button>
    </>
  );
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

it.each(["Settings", "Other session"])(
  "confirms and cancels %s before destroying the editor",
  (action) => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    render(
      <MemoryRouter initialEntries={["/c/one"]}>
        <Routes>
          <Route path="/c/one" element={<EditorRoute />} />
          <Route path="*" element={<p>Destination</p>} />
        </Routes>
      </MemoryRouter>,
    );
    const frame = screen.getByTitle("Retained editor");
    fireEvent.click(screen.getByText(action));
    expect(screen.getByTitle("Retained editor")).toBe(frame);
    expect(confirm).toHaveBeenCalledTimes(1);
    confirm.mockReturnValue(true);
    fireEvent.click(screen.getByText(action));
    expect(screen.getByText("Destination")).toBeInTheDocument();
    expect(frame).not.toBeInTheDocument();
  },
);

it("does not ask for same-route query updates", () => {
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  render(
    <MemoryRouter initialEntries={["/c/one"]}>
      <Routes>
        <Route path="*" element={<EditorRoute />} />
      </Routes>
    </MemoryRouter>,
  );
  const frame = screen.getByTitle("Retained editor");
  fireEvent.click(screen.getByText("Same route"));
  expect(screen.getByText("/c/one?panel=docs")).toBeInTheDocument();
  expect(screen.getByTitle("Retained editor")).toBe(frame);
  expect(confirm).not.toHaveBeenCalled();
});

it("cancels BrowserRouter Back before its listener and restores the previous history index", () => {
  const previous = window.location.href;
  const state = window.history.state;
  window.history.replaceState({ idx: 2 }, "", "/c/current");
  const ask = vi.fn(() => false);
  const unregister = registerRouteLeaveGuard(ask);
  const go = vi.spyOn(window.history, "go").mockImplementation(() => {});
  const router = vi.fn();
  window.addEventListener("popstate", router);
  try {
    window.history.replaceState({ idx: 1 }, "", "/c/previous");
    window.dispatchEvent(new PopStateEvent("popstate", { state: { idx: 1 } }));
    expect(ask).toHaveBeenCalledOnce();
    expect(go).toHaveBeenCalledWith(1);
    expect(router).not.toHaveBeenCalled();
    window.history.replaceState({ idx: 2 }, "", "/c/current");
    window.dispatchEvent(new PopStateEvent("popstate", { state: { idx: 2 } }));
    expect(ask).toHaveBeenCalledOnce();
    expect(router).toHaveBeenCalledOnce();
  } finally {
    unregister();
    window.removeEventListener("popstate", router);
    window.history.replaceState(state, "", previous);
  }
});
