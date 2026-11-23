import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { type ReactNode, useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";
import { workspaceOpenCommands, type WorkspaceCommandFacts } from "@/shell/workspaceCommands/model";
import { HIDDEN, unavailableBecause } from "@/store/paletteCommands";

import { shortcutKeys } from "@/shell/workspaceTabs/workspaceSidebarShortcuts";

import { NewTabPage, type NewTabPlace, type NewTabRecent } from "./NewTabPage";

const baseFacts: WorkspaceCommandFacts = {
  openablePanels: [
    { tab: "files", label: "Files" },
    {
      tab: "changes",
      label: "Diffs",
      availability: unavailableBecause("This folder isn't a git repository."),
    },
    { tab: "github", label: "Pull Requests" },
    { tab: "subagents", label: "Agents" },
  ],
  browser: true,
  sideChat: true,
  shells: {
    declared: ["zsh", "bash"],
    defaultShell: "zsh",
    connect: "ready",
    launching: false,
    canEdit: true,
  },
  openPanels: [],
  boundChords: { shell: true, browser: true },
  fullScreen: null,
};

function renderPage({
  initialPlace = { kind: "new" } as NewTabPlace,
  facts = baseFacts,
  textSearch = false,
  recents = undefined as readonly NewTabRecent[] | undefined,
  sandbox = undefined as Parameters<typeof NewTabPage>[0]["sandbox"],
  notice = undefined as ReactNode,
} = {}) {
  const onOpenRecent = vi.fn();
  const onOpenPlace = vi.fn();
  const onRunCommand = vi.fn();
  const onTextSearch = vi.fn();
  const onDismiss = vi.fn();
  const onPlaceChange = vi.fn();
  let setPlaceFromOutside: (place: NewTabPlace) => void = () => {};
  function Harness() {
    const [place, setPlace] = useState<NewTabPlace>(initialPlace);
    setPlaceFromOutside = setPlace;
    return (
      <NewTabPage
        place={place}
        onPlaceChange={(next) => {
          onPlaceChange(next);
          setPlace(next);
        }}
        commands={workspaceOpenCommands(facts)}
        textSearch={textSearch}
        onTextSearch={onTextSearch}
        onOpenPlace={onOpenPlace}
        onRunCommand={onRunCommand}
        onDismiss={onDismiss}
        recents={recents}
        onOpenRecent={onOpenRecent}
        sandbox={sandbox}
        notice={notice}
      />
    );
  }
  render(
    <TooltipProvider>
      <Harness />
    </TooltipProvider>,
  );
  return {
    onOpenRecent,
    onOpenPlace,
    onRunCommand,
    onTextSearch,
    onDismiss,
    onPlaceChange,
    setPlace: (place: NewTabPlace) => setPlaceFromOutside(place),
  };
}

/** More shells than get rows of their own, so "Choose shell…" lists the rest. */
const manyShellFacts: WorkspaceCommandFacts = {
  ...baseFacts,
  shells: { ...baseFacts.shells!, declared: ["zsh", "bash", "fish", "nu", "sh"] },
};

const appList = () => screen.queryByRole("combobox", { name: "Open an app in this tab" });
const shellList = () => screen.queryByRole("combobox", { name: "Choose a shell" });

afterEach(cleanup);

describe("NewTabPage", () => {
  it("focuses the filter and starts the default shell on Enter", async () => {
    const { onRunCommand } = renderPage();
    expect(appList()).toHaveFocus();
    await userEvent.type(appList()!, "terminal");
    await userEvent.keyboard("{Enter}");
    expect(onRunCommand).toHaveBeenCalledWith(
      expect.objectContaining({ kind: "shell", shell: "zsh" }),
    );
  });

  it("filters by typing and opens Files in the tab", async () => {
    const { onOpenPlace } = renderPage();
    await userEvent.keyboard("files{Enter}");
    expect(onOpenPlace).toHaveBeenCalledWith({ kind: "files" });
  });

  it("leaves hidden apps out entirely", () => {
    renderPage({
      facts: {
        ...baseFacts,
        openablePanels: [
          { tab: "files", label: "Files" },
          { tab: "changes", label: "Diffs", availability: HIDDEN },
        ],
      },
    });
    expect(screen.queryByRole("option", { name: /^Diffs/ })).toBeNull();
    expect(screen.queryByRole("option", { name: /^Search text/ })).toBeNull();
    expect(screen.getByRole("option", { name: /^Files/ })).toBeInTheDocument();
  });

  it("opens the workspace text search from Search text, with its shortcut, and leaves the tab", async () => {
    const { onTextSearch, onOpenPlace, onRunCommand } = renderPage({ textSearch: true });
    const row = screen.getByRole("option", { name: /^Search text/ });
    expect(row).not.toHaveAttribute("data-layout", "card");
    expect(row).toHaveTextContent(shortcutKeys("palette.text")!.join(""));
    await userEvent.click(row);
    expect(onTextSearch).toHaveBeenCalledTimes(1);
    expect(onOpenPlace).not.toHaveBeenCalled();
    expect(onRunCommand).not.toHaveBeenCalled();
  });

  it("shows the new-shell chord on the Terminal row", () => {
    renderPage();
    const terminal = screen.getByRole("option", { name: /^Terminal/ });
    expect(terminal).toHaveTextContent(shortcutKeys("terminal.new")!.join(""));
    cleanup();
    // Where the app's chords act on another session, the card shows none.
    renderPage({ facts: { ...baseFacts, boundChords: { shell: false, browser: false } } });
    expect(screen.getByRole("option", { name: /^Terminal/ })).not.toHaveTextContent(
      shortcutKeys("terminal.new")!.join(""),
    );
  });

  it("starts another shell from its own row in one click", async () => {
    const { onRunCommand, onPlaceChange } = renderPage();
    expect(screen.queryByRole("option", { name: /Choose shell/ })).toBeNull();
    await userEvent.click(screen.getByRole("option", { name: /^bash/ }));
    expect(onRunCommand).toHaveBeenCalledWith(
      expect.objectContaining({ kind: "shell", shell: "bash" }),
    );
    expect(onPlaceChange).not.toHaveBeenCalled();
  });

  it("reports the chooser as the tab's place and goes back with Escape, keeping the app list's query", async () => {
    const { onPlaceChange, onDismiss, onRunCommand } = renderPage({ facts: manyShellFacts });
    await userEvent.keyboard("sh");
    await userEvent.click(screen.getByRole("option", { name: /Choose shell/ }));
    expect(onPlaceChange).toHaveBeenLastCalledWith({ kind: "new", app: "terminal" });
    expect(shellList()).toHaveValue("");
    await userEvent.keyboard("{Escape}");
    expect(onPlaceChange).toHaveBeenLastCalledWith({ kind: "new" });
    expect(onDismiss).not.toHaveBeenCalled();
    expect(appList()).toHaveValue("sh");
    await userEvent.click(screen.getByRole("option", { name: /Choose shell/ }));
    await userEvent.click(screen.getByRole("option", { name: /^sh$/ }));
    expect(onRunCommand).toHaveBeenCalledWith(
      expect.objectContaining({ kind: "shell", shell: "sh" }),
    );
  });

  it("goes back to the apps with Backspace on the chooser's empty query, without dismissing", async () => {
    const { onPlaceChange, onDismiss } = renderPage({ facts: manyShellFacts });
    await userEvent.click(screen.getByRole("option", { name: /Choose shell/ }));
    expect(onPlaceChange).toHaveBeenLastCalledWith({ kind: "new", app: "terminal" });
    await userEvent.keyboard("{Backspace}");
    expect(onPlaceChange).toHaveBeenLastCalledWith({ kind: "new" });
    expect(onDismiss).not.toHaveBeenCalled();
  });

  it("marks Choose shell… as opening a page of its own", () => {
    renderPage({ facts: manyShellFacts });
    const chooser = screen.getByRole("option", { name: /Choose shell/ });
    expect(chooser.querySelector(".lucide-chevron-right")).not.toBeNull();
    expect(
      screen.getByRole("option", { name: /^Terminal/ }).querySelector(".lucide-chevron-right"),
    ).toBeNull();
  });

  it("opens straight into the chooser for omnigent://new?app=terminal and follows outside place changes", async () => {
    const { setPlace } = renderPage({ initialPlace: { kind: "new", app: "terminal" } });
    expect(shellList()).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /zsh \(default\)/ })).toBeInTheDocument();
    setPlace({ kind: "new" });
    expect(
      await screen.findByRole("combobox", { name: "Open an app in this tab" }),
    ).toBeInTheDocument();
    setPlace({ kind: "new", app: "terminal" });
    expect(await screen.findByRole("combobox", { name: "Choose a shell" })).toBeInTheDocument();
  });

  it("opens the chooser with Right Arrow on Terminal without starting a shell", async () => {
    const { onRunCommand, onPlaceChange } = renderPage();
    await userEvent.type(appList()!, "terminal");
    await userEvent.keyboard("{ArrowRight}");
    expect(onPlaceChange).toHaveBeenLastCalledWith({ kind: "new", app: "terminal" });
    expect(shellList()).toBeInTheDocument();
    expect(onRunCommand).not.toHaveBeenCalled();
  });

  it("keeps Right Arrow for the caret while editing the query", async () => {
    const { onPlaceChange } = renderPage();
    await userEvent.keyboard("term{ArrowLeft}{ArrowRight}");
    expect(onPlaceChange).not.toHaveBeenCalled();
    expect(appList()).toHaveValue("term");
    await userEvent.keyboard("{ArrowRight}");
    expect(onPlaceChange).toHaveBeenLastCalledWith({ kind: "new", app: "terminal" });
  });

  it("does nothing on Right Arrow when there's only one shell", async () => {
    const { onPlaceChange } = renderPage({
      facts: { ...baseFacts, shells: { ...baseFacts.shells!, declared: ["zsh"] } },
    });
    await userEvent.keyboard("{ArrowRight}");
    expect(onPlaceChange).not.toHaveBeenCalled();
  });

  it("lets the best match take Enter after typing, even when the row picked before typing still matches", async () => {
    const { onRunCommand, onPlaceChange } = renderPage();
    await userEvent.keyboard("{ArrowDown}zsh{Enter}");
    expect(onPlaceChange).not.toHaveBeenCalled();
    expect(onRunCommand).toHaveBeenCalledWith(
      expect.objectContaining({ kind: "shell", shell: "zsh" }),
    );
  });

  it("dismisses from the app list with Escape", async () => {
    const { onDismiss } = renderPage();
    await userEvent.keyboard("{Escape}");
    expect(onDismiss).toHaveBeenCalled();
  });

  it("goes back from the chooser with Escape from its Back button, without dismissing", async () => {
    const { onPlaceChange, onDismiss } = renderPage();
    await userEvent.type(appList()!, "terminal");
    await userEvent.keyboard("{ArrowRight}");
    expect(shellList()).toBeInTheDocument();
    screen.getByRole("button", { name: "Back to apps" }).focus();
    await userEvent.keyboard("{Escape}");
    expect(onPlaceChange).toHaveBeenLastCalledWith({ kind: "new" });
    expect(onDismiss).not.toHaveBeenCalled();
    expect(appList()).toHaveFocus();
  });

  it("opens a blank browser page and a side-chat draft", async () => {
    const { onOpenPlace } = renderPage();
    await userEvent.click(screen.getByRole("option", { name: /^Browser/ }));
    await userEvent.click(screen.getByRole("option", { name: /^Side chat/ }));
    expect(onOpenPlace.mock.calls).toEqual([[{ kind: "web" }], [{ kind: "chat" }]]);
  });
});

describe("NewTabPage sections", () => {
  const recents: NewTabRecent[] = [
    { identity: "files:src/app.ts", label: "app.ts", description: "src", icon: null },
    { identity: "pulls", label: "Pull Requests", icon: null },
  ];
  const groups = () => screen.queryAllByRole("group");
  const headings = () =>
    groups().map(
      (group) => document.getElementById(group.getAttribute("aria-labelledby") ?? "")?.textContent,
    );
  const labelsIn = (group: HTMLElement) =>
    Array.from(group.querySelectorAll("[role=option] [data-slot=quick-pick-label]")).map(
      (label) => label.textContent,
    );

  it("lists the app cards, then the other apps, then recent places", () => {
    renderPage({ recents });
    expect(headings()).toEqual(["Apps", "More", "Recent"]);
    const options = screen.getAllByRole("option");
    expect(options[0]).toHaveTextContent("Side chat");
    expect(options.at(-1)).toHaveTextContent("Pull Requests");
    expect(labelsIn(groups()[0]!)).toEqual(["Side chat", "Diffs", "Files", "Pull Requests"]);
    const cards = Array.from(document.querySelectorAll("[data-layout=card]")).map(
      (card) => card.querySelector("[data-slot=quick-pick-label]")?.textContent,
    );
    expect(cards).toEqual(["Side chat", "Diffs", "Files", "Pull Requests"]);
    expect(screen.getAllByRole("option", { name: /^Pull Requests/ })[0]).toHaveTextContent(
      "Review the pull requests for this workspace",
    );
    expect(screen.getByRole("option", { name: /^Side chat/ })).toHaveTextContent(
      "Ask a quick question on the side",
    );
    expect(screen.getByRole("option", { name: /^Files/ })).toHaveTextContent(
      "Browse and open the workspace's files",
    );
    expect(labelsIn(groups()[1]!).slice(0, 2)).toEqual(["Terminal", "bash"]);
  });

  it("shows a recent pull request's number at the trailing edge and finds it by author", async () => {
    const { onOpenRecent } = renderPage({
      recents: [
        {
          identity: "pull:https://github.com/acme/widgets/pull/338",
          label: "Show who wrote each review comment",
          description: "jane-doe · acme/widgets",
          detail: "#338",
          keywords: ["jane-doe", "acme/widgets", "#338", "338"],
          icon: null,
        },
      ],
    });
    const row = screen.getByRole("option", { name: /Show who wrote each review comment/ });
    expect(row).toHaveTextContent("jane-doe · acme/widgets");
    expect(row).toHaveTextContent("#338");
    await userEvent.type(appList()!, "jane");
    await userEvent.keyboard("{Enter}");
    expect(onOpenRecent).toHaveBeenCalledWith("pull:https://github.com/acme/widgets/pull/338");
  });

  it("shows the page's notice while a stopped sandbox resumes, and leaves the Terminal row a choice", () => {
    renderPage({ sandbox: "resuming", notice: <p>Resuming sandbox…</p> });
    expect(screen.getByText("Resuming sandbox…")).toBeInTheDocument();
    // Still a choice: the shell starts as the sandbox comes back.
    expect(screen.getByRole("option", { name: /^Terminal/ })).not.toHaveAttribute("aria-disabled");
  });

  it("hides Recent when there's nothing to go back to, and starts on the first card", () => {
    renderPage({ recents: [] });
    expect(headings()).toEqual(["Apps", "More"]);
    const input = appList()!;
    const active = document.getElementById(input.getAttribute("aria-activedescendant") ?? "");
    expect(active).toHaveTextContent("Side chat");
  });

  it("goes to a recent place when clicked, by its identity", async () => {
    const { onOpenRecent } = renderPage({ recents });
    await userEvent.click(screen.getByRole("option", { name: /^app\.ts/ }));
    expect(onOpenRecent).toHaveBeenCalledWith("files:src/app.ts");
  });

  it("filters apps and recent places together, and says when nothing matches", async () => {
    renderPage({ recents });
    await userEvent.type(appList()!, "pull");
    const labels = screen
      .getAllByRole("option")
      .map((option) => option.querySelector("[data-slot=quick-pick-label]")?.textContent);
    expect(labels).toEqual(expect.arrayContaining(["Pull Requests"]));
    await userEvent.clear(appList()!);
    await userEvent.type(appList()!, "zzzz");
    expect(screen.getAllByText("No app or recent place matches.").length).toBeGreaterThan(0);
  });

  it("puts an exact app first, above a recent place that only starts with its name, and Enter opens it", async () => {
    const { onOpenRecent, onRunCommand } = renderPage({
      recents: [
        { identity: "files:notes/Terminal notes.md", label: "Terminal notes.md", icon: null },
      ],
    });
    await userEvent.type(appList()!, "Terminal");
    const labels = screen
      .getAllByRole("option")
      .map((option) => option.querySelector("[data-slot=quick-pick-label]")?.textContent);
    expect(labels[0]).toBe("Terminal");
    expect(labels).toContain("Terminal notes.md");
    await userEvent.keyboard("{Enter}");
    expect(onRunCommand).toHaveBeenCalledWith(expect.objectContaining({ kind: "shell" }));
    expect(onOpenRecent).not.toHaveBeenCalled();
  });

  it("ranks across sections, so a recent place matching the query exactly comes before an app that only starts with it", async () => {
    const { onOpenRecent } = renderPage({
      recents: [{ identity: "files:docs/pull", label: "pull", icon: null }],
    });
    await userEvent.type(appList()!, "pull");
    const labels = screen
      .getAllByRole("option")
      .map((option) => option.querySelector("[data-slot=quick-pick-label]")?.textContent);
    expect(labels.slice(0, 2)).toEqual(["pull", "Pull Requests"]);
    await userEvent.keyboard("{Enter}");
    expect(onOpenRecent).toHaveBeenCalledWith("files:docs/pull");
  });

  it("goes to a recent place that is the best match for the query", async () => {
    const { onOpenRecent } = renderPage({ recents });
    await userEvent.type(appList()!, "app.ts");
    await userEvent.keyboard("{Enter}");
    expect(onOpenRecent).toHaveBeenCalledWith("files:src/app.ts");
  });

  it("shows a disabled app's reason on its card, skips it with the arrows and doesn't open it", async () => {
    const { onOpenPlace } = renderPage({ recents: [] });
    expect(screen.getByRole("option", { name: /^Diffs/ })).toHaveTextContent(
      "This folder isn't a git repository.",
    );
    // Side chat → (Diffs is disabled) → Files.
    await userEvent.keyboard("{ArrowDown}");
    const input = appList()!;
    const active = document.getElementById(input.getAttribute("aria-activedescendant") ?? "");
    expect(active).toHaveTextContent("Files");

    await userEvent.click(
      screen.getByRole("option", { name: /Diffs.*This folder isn't a git repository/ }),
    );
    expect(onOpenPlace).not.toHaveBeenCalled();
  });
});
