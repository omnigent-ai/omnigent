import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

const catalog = vi.hoisted(() => ({ loading: true }));

vi.mock("./ExtensionProvider", () => ({
  useExtensions: () => [],
  useExtensionsLoading: () => catalog.loading,
}));
vi.mock("@/pages/NotFoundPage", () => ({
  NotFoundPage: () => <div>page not found</div>,
}));
vi.mock("./ExtensionPageHost", () => ({
  ExtensionPageHost: () => <div>extension page</div>,
}));

import { ExtensionPageRoute } from "./ExtensionPageRoute";

function renderRoute() {
  return render(
    <MemoryRouter initialEntries={["/extensions/omnigent.canvas/canvas"]}>
      <Routes>
        <Route path="/extensions/:extensionId/*" element={<ExtensionPageRoute />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  catalog.loading = true;
});

describe("ExtensionPageRoute", () => {
  it("shows only a spinner while the extension catalog is unresolved", () => {
    renderRoute();

    expect(screen.getByRole("status", { name: "Loading extension" })).toBeInTheDocument();
    expect(screen.queryByText("page not found")).toBeNull();
  });

  it("shows not found after the extension catalog resolves", () => {
    catalog.loading = false;
    renderRoute();

    expect(screen.getByText("page not found")).toBeInTheDocument();
    expect(screen.queryByRole("status")).toBeNull();
  });
});
