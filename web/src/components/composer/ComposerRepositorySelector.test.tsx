import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ComposerRepositorySelection } from "@/lib/composerContext";
import { ComposerRepositorySelector } from "./ComposerRepositorySelector";

const app: ComposerRepositorySelection = {
  id: "app",
  url: "https://github.com/acme/application.git",
  branch: "main",
};
const docs: ComposerRepositorySelection = {
  id: "docs",
  url: "https://github.com/acme/documentation.git",
  branch: null,
};

afterEach(cleanup);

describe("ComposerRepositorySelector", () => {
  it("represents intentionally empty context explicitly", async () => {
    const onChange = vi.fn();
    render(
      <ComposerRepositorySelector
        value={[app]}
        repositories={{ status: "ready", data: [app, docs], error: null }}
        onChange={onChange}
      />,
    );

    await userEvent.click(
      screen.getByRole("button", { name: "Repositories, 1 repository selected" }),
    );
    await userEvent.click(screen.getByText("No repositories"));
    expect(onChange).toHaveBeenCalledWith([]);
  });

  it("appends new selections and preserves existing order and identity", async () => {
    const onChange = vi.fn();
    render(
      <ComposerRepositorySelector
        value={[docs]}
        repositories={{ status: "ready", data: [app, docs], error: null }}
        onChange={onChange}
      />,
    );

    await userEvent.click(
      screen.getByRole("button", { name: "Repositories, 1 repository selected" }),
    );
    await userEvent.click(screen.getByText("application"));
    expect(onChange).toHaveBeenCalledWith([docs, app]);
  });

  it("renders ordered chips with accessible removal controls", async () => {
    const onChange = vi.fn();
    render(
      <ComposerRepositorySelector
        value={[docs, app]}
        repositories={{ status: "ready", data: [app, docs], error: null }}
        onChange={onChange}
      />,
    );

    const selector = screen.getByTestId("composer-repository-selector");
    expect(
      within(selector)
        .getAllByTitle(/github\.com/)
        .map((node) => node.textContent),
    ).toEqual(["documentation", "application"]);
    await userEvent.click(screen.getByRole("button", { name: "Remove documentation" }));
    expect(onChange).toHaveBeenCalledWith([app]);
  });

  it("supports keyboard search and option navigation", async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(
      <ComposerRepositorySelector
        value={[]}
        repositories={{ status: "ready", data: [app, docs], error: null }}
        onChange={onChange}
      />,
    );

    await user.tab();
    expect(
      screen.getByRole("button", { name: "Repositories, no repositories selected" }),
    ).toHaveFocus();
    await user.keyboard("{Enter}");
    const search = screen.getByRole("combobox", { name: "Search repositories" });
    await user.type(search, "documentation");
    await user.keyboard("{ArrowDown}{Enter}");
    expect(onChange).toHaveBeenCalledWith([docs]);
  });

  it("shows loading, unavailable, stale, and error resource states", () => {
    const { rerender } = render(
      <ComposerRepositorySelector
        value={[]}
        repositories={{ status: "loading", data: null, error: null }}
        onChange={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Repositories, no repositories selected" }));
    expect(screen.getByText("Loading repositories…")).toBeInTheDocument();

    rerender(
      <ComposerRepositorySelector
        value={[]}
        repositories={{ status: "unavailable", data: null, error: null }}
        onChange={vi.fn()}
      />,
    );
    expect(screen.getByText("Repository selection is unavailable.")).toBeInTheDocument();

    rerender(
      <ComposerRepositorySelector
        value={[]}
        repositories={{ status: "stale", data: [app], error: null }}
        onChange={vi.fn()}
      />,
    );
    expect(screen.getByText("Repository list may be out of date.")).toBeInTheDocument();

    rerender(
      <ComposerRepositorySelector
        value={[]}
        repositories={{ status: "error", data: null, error: new Error("Host disconnected") }}
        onChange={vi.fn()}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("Host disconnected");
  });

  it("keeps selected repositories visible when they disappear from refreshed options", async () => {
    render(
      <ComposerRepositorySelector
        value={[docs]}
        repositories={{ status: "error", data: [app], error: new Error("Refresh failed") }}
        onChange={vi.fn()}
      />,
    );

    await userEvent.click(
      screen.getByRole("button", { name: "Repositories, 1 repository selected" }),
    );
    expect(screen.getByText("documentation")).toBeInTheDocument();
    expect(screen.getByText("Unavailable")).toBeInTheDocument();
  });
});
