import { act, cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { SessionImportStatus } from "./SessionImportStatus";
import { useSessionImportStore } from "@/store/sessionImport";

function renderStatus() {
  return render(
    <MemoryRouter>
      <SessionImportStatus />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  useSessionImportStore.setState({
    importing: false,
    importedCount: 0,
    request: null,
    result: null,
    error: null,
  });
});

afterEach(cleanup);

describe("SessionImportStatus", () => {
  it("shows the running import with its count, links to the panel, and clears when done", () => {
    renderStatus();
    expect(screen.queryByTestId("sidebar-import-progress")).toBeNull();

    act(() => useSessionImportStore.setState({ importing: true, importedCount: 0 }));
    const status = screen.getByTestId("sidebar-import-progress");
    expect(status).toHaveTextContent("Importing (0)");
    expect(screen.getByRole("link", { name: /Importing sessions/ })).toHaveAttribute(
      "href",
      "/settings/import",
    );

    act(() =>
      useSessionImportStore.setState({
        importedCount: 3,
      }),
    );
    expect(status).toHaveTextContent("Importing (3)");

    act(() => useSessionImportStore.setState({ importing: false }));
    expect(screen.queryByTestId("sidebar-import-progress")).toBeNull();
  });
});
