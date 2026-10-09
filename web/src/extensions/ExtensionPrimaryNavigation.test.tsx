import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";

const extensions = [
  {
    object: "extension" as const,
    id: "acme.review",
    display_name: "Acme Review",
    distribution: "acme-review",
    version: "1.0.0",
    extension_api: 1,
    status: "enabled" as const,
    permissions: [],
    pages: [
      { id: "acme.review.second", title: "Second", route: "second", view: "second" },
      { id: "acme.review.first", title: "First", route: "first", view: "first" },
    ],
    primary_navigation: [
      {
        id: "acme.review.second-nav",
        label: "Second",
        page: "acme.review.second",
        icon: null,
        order: 500,
        when: null,
      },
      {
        id: "acme.review.first-nav",
        label: "First",
        page: "acme.review.first",
        icon: "unknown-icon",
        order: 300,
        when: null,
      },
    ],
    browser: { declared: true, has_styles: false, digest: "x", script_url: "/x", style_url: null },
  },
];

vi.mock("./ExtensionProvider", () => ({ useExtensions: () => extensions }));

import { ExtensionPrimaryNavigation } from "./ExtensionPrimaryNavigation";

describe("ExtensionPrimaryNavigation", () => {
  it("keeps compact links accessible and interactive without visible labels", () => {
    const onNavigate = vi.fn();
    render(
      <TooltipProvider>
        <MemoryRouter>
          <ExtensionPrimaryNavigation
            activePageId="acme.review.first"
            onNavigate={onNavigate}
            compact
          />
        </MemoryRouter>
      </TooltipProvider>,
    );

    const first = screen.getByRole("link", { name: "First" });
    const second = screen.getByRole("link", { name: "Second" });
    expect(first).toHaveAttribute("href", "/extensions/acme.review/first");
    expect(first).toHaveAttribute("aria-current", "page");
    expect(second).toHaveAttribute("href", "/extensions/acme.review/second");
    expect(first.textContent).toBe("");
    expect(second.textContent).toBe("");
    fireEvent.click(first);
    expect(onNavigate).toHaveBeenCalledOnce();
  });

  it("orders entries, falls back unknown icons, and marks the active page", () => {
    const onNavigate = vi.fn();
    render(
      <MemoryRouter>
        <ExtensionPrimaryNavigation activePageId="acme.review.first" onNavigate={onNavigate} />
      </MemoryRouter>,
    );

    const links = screen.getAllByRole("link");
    expect(links.map((link) => link.textContent)).toEqual(["First", "Second"]);
    expect(links[0]).toHaveAttribute("href", "/extensions/acme.review/first");
    expect(links[0]).toHaveAttribute("aria-current", "page");
    expect(links[0].querySelector("svg")).not.toBeNull();

    fireEvent.click(links[0]);
    expect(onNavigate).toHaveBeenCalledOnce();
  });
});
