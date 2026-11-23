import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";
import { ProjectSessionPanel } from "./ProjectSessionPanel";

function SessionLocation() {
  return <output aria-label="Session route">{useLocation().pathname}</output>;
}
afterEach(cleanup);

describe("Legacy project session links", () => {
  it.each(["planner", "a b/c&d"])(
    "redirects %s to the normal session without mounting an embedded chat",
    (sessionId) => {
      render(
        <MemoryRouter initialEntries={["/projects/Example?ticket=ticket&session=old"]}>
          <Routes>
            <Route
              path="/projects/:projectName"
              element={<ProjectSessionPanel sessionId={sessionId} />}
            />
            <Route path="/c/:sessionId" element={<SessionLocation />} />
          </Routes>
        </MemoryRouter>,
      );
      expect(screen.getByLabelText("Session route")).toHaveTextContent(
        `/c/${encodeURIComponent(sessionId)}`,
      );
      expect(screen.queryByTestId("project-session-panel")).toBeNull();
      expect(screen.queryByRole("link", { name: "Back to ticket" })).toBeNull();
    },
  );
});
