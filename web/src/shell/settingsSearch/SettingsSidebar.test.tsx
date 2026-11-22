import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SettingsSidebar } from "./SettingsSidebar";

afterEach(cleanup);

function renderSidebar(onCollapse?: () => void) {
  render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <MemoryRouter initialEntries={["/settings/general"]}>
        <SettingsSidebar onNavClick={vi.fn()} onCollapse={onCollapse} onNavigate={vi.fn()} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return screen.getByRole("combobox", { name: "Search settings" });
}

describe("SettingsSidebar", () => {
  it("shows the search field under Back and above the section nav", () => {
    renderSidebar();
    const back = screen.getByRole("link", { name: /Back/ });
    const field = screen.getByRole("combobox", { name: "Search settings" });
    const nav = screen.getByRole("navigation");
    expect(back.compareDocumentPosition(field) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(field.compareDocumentPosition(nav) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("replaces the section nav with results while there is a query", () => {
    const field = renderSidebar();
    expect(screen.getByRole("navigation")).toBeInTheDocument();
    fireEvent.change(field, { target: { value: "terminal theme" } });
    expect(screen.queryByRole("navigation")).toBeNull();
    expect(screen.getByRole("option", { name: /Terminal theme/ })).toBeInTheDocument();
    fireEvent.change(field, { target: { value: "" } });
    expect(screen.getByRole("navigation")).toBeInTheDocument();
  });

  it("keeps the docked menu's Close sidebar button next to Back", () => {
    const onCollapse = vi.fn();
    renderSidebar(onCollapse);
    fireEvent.click(screen.getByRole("button", { name: "Close sidebar" }));
    expect(onCollapse).toHaveBeenCalledOnce();
  });

  it("hides Close sidebar when the menu doesn't dock", () => {
    renderSidebar();
    expect(screen.queryByRole("button", { name: "Close sidebar" })).toBeNull();
  });
});
