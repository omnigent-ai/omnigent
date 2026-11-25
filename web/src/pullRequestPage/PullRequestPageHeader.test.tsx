import { computeAccessibleDescription, computeAccessibleName } from "dom-accessibility-api";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  act,
  fireEvent,
  render as renderUi,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { type ReactElement, type ReactNode, useState } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { Tabs, TabsContent } from "@/components/ui/tabs";
import { gitProviderCopy } from "@/lib/gitProviders";
import {
  EXAMPLE_STACK_FORMAT,
  withPullRequestIntegrations,
  exampleMergeReadinessSource,
} from "@/test/pullRequestIntegrations";
import { parseStack } from "@/lib/pullRequestStacks/stackFormats";
import {
  pullRequestStatusDisplay,
  type PullRequestStatusDisplay,
} from "@/lib/pullRequestStatusDisplay";
import type { PullRequestSubtab } from "@/shell/pullRequestPanelView";

import type { PullRequestDetailsState } from "./detailsLayout";
import type * as HeaderFitModule from "./pullRequestHeaderFit";
import type { PullRequestHeaderFit } from "./pullRequestHeaderFit";
import {
  focusSectionControl,
  PullRequestPageHeader,
  PullRequestPagePreviewHeader,
} from "./PullRequestPageHeader";
import { STACK_LOADING } from "./PullRequestStack";
import type { ReadSource } from "./useReadSource";
import { MERGE_SECTION_ATTRIBUTE } from "./PullRequestMergeStatusBadge";
import { pullRequestStackView } from "./usePullRequestStack";

// Merge readiness from a stand-in source, as a build that registers one shows it.
withPullRequestIntegrations({
  mergeReadiness: exampleMergeReadinessSource(async () => ({ kind: "checking" })),
});

// Stack descriptions in an example format, which the page reads once a build registers it.
const STACK_FORMATS = [EXAMPLE_STACK_FORMAT];
withPullRequestIntegrations({ stackFormats: STACK_FORMATS });

// Outside the Pull Requests page, a PR has no page account choices.
// The state pill's menu and the lifecycle owner have their own tests; here, just the pill.
vi.mock("./PullRequestLifecycleControls", () => ({
  PullRequestStateMenu: ({ badge }: { badge: React.ReactNode }) => badge,
  usePullRequestStatePillProgress: () => ({ kind: "idle" }),
  PullRequestLifecycleOwner: () => null,
  PullRequestChangeNotice: () => <p>What the last change came to</p>,
}));

const fit = vi.hoisted(() => ({
  current: {
    name: "full",
    status: "full",
    source: "full",
    sections: "segments",
  } as PullRequestHeaderFit,
}));
vi.mock("./pullRequestHeaderFit", async (importOriginal) => ({
  ...(await importOriginal<typeof HeaderFitModule>()),
  usePullRequestHeaderFit: () => fit.current,
}));

const MENU: PullRequestHeaderFit = {
  name: "repository",
  spacing: "roomy",
  lifecycle: "full",
  stack: "full",
  status: "short",
  source: "short",
  sections: "menu",
};
const SEGMENTS: PullRequestHeaderFit = {
  name: "full",
  spacing: "roomy",
  lifecycle: "full",
  stack: "full",
  status: "full",
  source: "full",
  sections: "segments",
};

// Where the PR is read; each test says what this Mac offers.
const readSource = vi.hoisted(() => ({ current: null as unknown as ReadSource }));
vi.mock("./useReadSource", () => ({ useReadSource: () => readSource.current }));
// The repository's account group has its own tests; these offer none.
vi.mock("./useRepoAccount", () => ({ useRepoAccount: () => null }));

function source(over: Partial<ReadSource> = {}): ReadSource {
  return {
    offered: true,
    choice: "automatic",
    route: { kind: "mac", target: {} as never },
    automaticPick: "mac",
    provider: "github",
    repo: { kind: "none" },
    host: "github.com",
    login: "octo",
    choose: vi.fn(),
    ...over,
  };
}

// The PR's status as the composer's chip names it; each test says what it is.
let mergeStatus: PullRequestStatusDisplay | undefined;
let freshness: PullRequestDetailsState["freshness"] = null;

function state(over: Partial<PullRequestDetailsState["tabs"]> = {}): PullRequestDetailsState {
  return {
    conversationId: "conv_1",
    copy: gitProviderCopy("github"),
    repository: "acme/app",
    pr: {
      number: 362,
      url: "https://github.com/acme/app/pull/362",
      state: "OPEN",
      isDraft: false,
      title: "Collect diagnostics",
    },
    selectedPrUrl: null,
    titleEditable: false,
    files: [{ path: "a.ts" }, { path: "b.ts" }] as unknown as PullRequestDetailsState["files"],
    mergeStatus,
    freshness,
    readPath: null,
    lifecycle: {
      conversationId: "conv_1",
      prUrl: "https://github.com/acme/app/pull/362",
      number: 362,
      state: "OPEN",
      isDraft: false,
      headSha: "a".repeat(40),
      autoMergeOn: false,
      mergeable: false,
    },
    active: true,
    tabs: {
      shown: "summary",
      select: () => {},
      commits: { shown: true, count: 7 },
      changes: { count: 12 },
      ...over,
    },
  } as unknown as PullRequestDetailsState;
}

// The header's Review menu reads the session's route, which a page always has a client for.
const queries = ({ children }: { children: ReactNode }) => (
  <QueryClientProvider client={new QueryClient()}>{children}</QueryClientProvider>
);
const render = (ui: ReactElement) => renderUi(ui, { wrapper: queries });

/** The header inside a Tabs root with a panel per section, as the details render it. */
function Harness({ initial = "summary" as PullRequestSubtab, onSelect = vi.fn() }) {
  const [shown, setShown] = useState<PullRequestSubtab>(initial);
  const select = (section: PullRequestSubtab) => {
    onSelect(section);
    setShown(section);
  };
  return (
    <Tabs value={shown} onValueChange={(value) => select(value as PullRequestSubtab)}>
      <PullRequestPageHeader state={state({ shown, select })} options={{}} />
      {(["summary", "commits", "changes"] as const).map((section) => (
        <TabsContent key={section} value={section}>
          {section} panel
          {section === "summary" && (
            <div {...{ [MERGE_SECTION_ATTRIBUTE]: "" }} tabIndex={-1} aria-label="Merge section" />
          )}
        </TabsContent>
      ))}
      <input aria-label="Title" />
    </Tabs>
  );
}

beforeEach(() => {
  fit.current = SEGMENTS;
  readSource.current = source();
  mergeStatus = undefined;
  freshness = null;
});

describe("PullRequestPageHeader", () => {
  it("shows the sections as tabs where they fit, with the full name kept in the link", () => {
    render(<Harness />);
    const tabs = screen.getByRole("tablist", { name: "Pull request" });
    expect(
      within(tabs)
        .getAllByRole("tab")
        .map((tab) => tab.textContent),
    ).toEqual(["Overview ", "Commits 7", "Changes 12"]);
    expect(screen.queryByRole("combobox")).toBeNull();
    expect(screen.getByRole("link", { name: "Open acme/app #362 on GitHub" })).toHaveTextContent(
      "acme/app #362",
    );
    expect(screen.getByLabelText("Pull request status: Open")).toHaveTextContent("Open");
  });

  it("draws the sections as underlined tabs with an icon, and measures the same faces", () => {
    const { container } = render(<Harness />);
    const tabs = screen.getByRole("tablist", { name: "Pull request" });
    expect(tabs).toHaveAttribute("data-variant", "line");
    for (const tab of within(tabs).getAllByRole("tab")) {
      expect(tab.querySelector("svg[aria-hidden][data-section-icon]")).not.toBeNull();
    }
    // The sizer measures what the tabs draw, so the fit agrees with what's shown.
    const sizer = container.querySelector<HTMLElement>(
      "[data-header-sizers] [data-header-sections]",
    )!;
    // Marked like the tabs' icons, so a narrow pane drops them from the measure too.
    expect(sizer.querySelectorAll("svg[data-section-icon]")).toHaveLength(3);
    expect([...sizer.children].map((face) => face.textContent)).toEqual(
      within(tabs)
        .getAllByRole("tab")
        .map((tab) => tab.textContent),
    );
  });

  it("offers a picker where the tabs don't fit, keeping every panel named", () => {
    fit.current = MENU;
    const onSelect = vi.fn();
    render(<Harness onSelect={onSelect} />);
    // The tabs stay behind the picker, out of reach, so each panel keeps its name.
    expect(screen.queryByRole("tablist")).toBeNull();
    expect(screen.getByRole("tabpanel")).toHaveAccessibleName("Overview");
    const picker = screen.getByRole("combobox", { name: "Pull request section" });
    expect(picker).toHaveTextContent("Overview");
    // A shorter name shows, while the link's name and tooltip keep the whole of it.
    const link = screen.getByRole("link", { name: "Open acme/app #362 on GitHub" });
    expect(link).toHaveTextContent(/^app #362$/);
    expect(link).toHaveAttribute("title", "Open acme/app #362 on GitHub");

    fireEvent.click(picker);
    // Open, the picker controls its own list, as a combobox should.
    expect(picker).toHaveAttribute("aria-expanded", "true");
    expect(picker).toHaveAttribute("aria-controls", screen.getByRole("listbox").id);
    const options = screen.getAllByRole("option").map((option) => option.textContent);
    expect(options).toEqual(["Overview", "Commits7", "Changes12"]);
    fireEvent.click(screen.getByRole("option", { name: /^Changes/ }));
    expect(onSelect).toHaveBeenCalledWith("changes");
    expect(screen.getByRole("tabpanel")).toHaveAccessibleName("Changes 12");
  });

  it("counts Commits and Changes with a pill, in the tabs and the picker", () => {
    const pills = (root: HTMLElement) =>
      [...root.querySelectorAll('[data-slot="badge"]')].map((pill) => pill.textContent);
    const view = render(<Harness />);
    const tabs = screen.getByRole("tablist", { name: "Pull request" });
    expect(pills(tabs)).toEqual(["7", "12"]);
    view.unmount();

    fit.current = MENU;
    render(<Harness initial="commits" />);
    const picker = screen.getByRole("combobox", { name: "Pull request section" });
    // The picker shows the current section's pill too.
    expect(pills(picker)).toEqual(["7"]);
    fireEvent.click(picker);
    expect(pills(screen.getByRole("listbox"))).toEqual(["7", "12"]);
  });

  it("measures copies that can't take focus or clash with the real ids", () => {
    const { container } = render(<Harness />);
    const sizers = container.querySelector("[data-header-sizers]")!;
    expect(sizers).toHaveAttribute("aria-hidden", "true");
    expect(sizers.querySelectorAll("button, a, input, [tabindex], [role], [id]")).toHaveLength(0);
    expect(sizers.querySelector('[data-header-name="number"]')).toHaveTextContent("#362");
    // The read source at each width it can take.
    expect(
      [...sizers.querySelectorAll("[data-header-source]")].map((sizer) => [
        sizer.getAttribute("data-header-source"),
        sizer.textContent,
      ]),
    ).toEqual([
      ["full", "This Mac · @octo"],
      ["short", "This Mac"],
      ["icon", ""],
    ]);
  });

  it("keeps focus on the sections when they swap between tabs and picker", () => {
    const view = render(<Harness />);
    act(() => screen.getByRole("tab", { name: "Overview" }).focus());
    fit.current = MENU;
    view.rerender(<Harness />);
    expect(screen.getByRole("combobox", { name: "Pull request section" })).toHaveFocus();
    fit.current = SEGMENTS;
    view.rerender(<Harness />);
    expect(screen.getByRole("tab", { name: "Overview" })).toHaveFocus();
  });

  it("leaves focus elsewhere alone when the sections swap", () => {
    const view = render(<Harness />);
    const title = screen.getByRole("textbox", { name: "Title" });
    act(() => title.focus());
    fit.current = MENU;
    view.rerender(<Harness />);
    expect(title).toHaveFocus();
  });
});

/** Focus the section control under `root`, reporting whether it took focus. */
function focusIn(root: HTMLElement): boolean {
  let focused = false;
  act(() => {
    focused = focusSectionControl(root);
  });
  return focused;
}

describe("focusSectionControl", () => {
  it("focuses the picker standing in for the tabs, else the selected tab", () => {
    fit.current = MENU;
    const view = render(<Harness />);
    expect(focusIn(view.container)).toBe(true);
    expect(screen.getByRole("combobox")).toHaveFocus();
    fit.current = SEGMENTS;
    view.rerender(<Harness />);
    expect(focusIn(view.container)).toBe(true);
    expect(screen.getByRole("tab", { name: "Overview" })).toHaveFocus();
  });

  it("says so when there's nothing to focus", () => {
    expect(focusSectionControl(document.createElement("div"))).toBe(false);
  });
});

describe("the header's read source", () => {
  const trigger = (name: RegExp | string = /^Read from /) => screen.getByRole("button", { name });
  const topRow = (view: ReturnType<typeof render>) =>
    view.container.querySelector<HTMLElement>("[data-header-row]")!;

  it("sits on the top row after the status, its whole account in the tooltip", () => {
    const view = render(<Harness />);
    const button = trigger("Read from This Mac · @octo. Change");
    expect(topRow(view)).toContainElement(button);
    expect(button).toHaveAttribute("title", "Read from This Mac · @octo");
    expect(button).toHaveTextContent("This Mac · @octo");
    // The status leads it; the sections are on the row below.
    const row = [...topRow(view).querySelectorAll("[data-header-part], button")];
    expect(row.indexOf(button)).toBeGreaterThan(
      row.indexOf(topRow(view).querySelector('[data-header-part="badges"]')!),
    );
    expect(topRow(view)).not.toContainElement(screen.getByRole("tablist"));
  });

  it("chooses from its menu from the keyboard, the current choice checked", async () => {
    const choose = vi.fn();
    readSource.current = source({ choose });
    render(<Harness />);
    const button = trigger();
    act(() => button.focus());
    await userEvent.keyboard("{Enter}");
    expect(screen.getByText("Read this pull request with")).toBeVisible();
    const items = screen.getAllByRole("menuitemradio");
    // Each choice is named by its label and says what it uses in its description.
    expect(
      items.map((item) => [computeAccessibleName(item), computeAccessibleDescription(item)]),
    ).toEqual([
      [
        "Automatic (this Mac)",
        "This Mac now. Follows Settings and whether this Mac can read github.com.",
      ],
      [
        "This Mac · @octo",
        "Uses gh on this Mac as @octo. Works while the session’s machine is off.",
      ],
      [
        "Session’s machine",
        "Uses gh on the session’s machine, as the account signed in there. Needs it running.",
      ],
    ]);
    // Read on this Mac while changes can't be made there: the menu says where they go.
    expect(screen.getByText("Changes are made through the session’s machine.")).toBeVisible();
    expect(items[0]).toHaveAttribute("aria-checked", "true");
    await userEvent.keyboard("{ArrowDown}{ArrowDown}{Enter}");
    expect(choose).toHaveBeenCalledWith("session");
    // Closed, focus goes back to the trigger.
    expect(screen.queryByRole("menu")).toBeNull();
    expect(button).toHaveFocus();
  });

  it("keeps just its icon where the header is narrowest, its name and tooltip whole", () => {
    fit.current = {
      name: "number",
      spacing: "roomy",
      lifecycle: "full",
      stack: "full",
      status: "short",
      source: "icon",
      sections: "menu",
    };
    render(<Harness />);
    const button = trigger("Read from This Mac · @octo. Change");
    expect(button).toHaveTextContent(/^$/);
    expect(button).toHaveAttribute("title", "Read from This Mac · @octo");
  });

  it("names the session's machine, and a Mac that can't read the PR", () => {
    readSource.current = source({ choice: "session", route: { kind: "host" } });
    const view = render(<Harness />);
    expect(trigger("Read from Session’s machine. Change")).toHaveTextContent("Session’s machine");
    view.unmount();

    readSource.current = source({
      choice: "mac",
      route: { kind: "blocked", reason: "mac_unsupported" },
    });
    render(<Harness />);
    const blocked = trigger("Read from This Mac, which can’t read it. Change");
    expect(blocked).toHaveTextContent(/^This Mac$/);
    expect(blocked).toHaveClass("text-warning");
  });

  it("isn't shown, or measured, where there's no choice to make", () => {
    // GitSpoke reads only on this Mac; a shell that can't read the host offers nothing.
    readSource.current = source({ offered: false, provider: "gitspoke", host: null, login: null });
    const view = render(<Harness />);
    expect(screen.queryByRole("button", { name: /^Read from / })).toBeNull();
    expect(view.container.querySelector("[data-header-source]")).toBeNull();
    expect(topRow(view)).toHaveTextContent("acme/app #362");
  });
});

describe("the header's merge status", () => {
  const conflicts = () =>
    pullRequestStatusDisplay(
      { state: "OPEN" },
      {
        kind: "known",
        readiness: {
          status: "conflicts",
          reasons: { failingJobs: [], failingRequirements: [] },
        },
        headSha: "abc",
      },
    );

  it("sits on the top row after the lifecycle pill and before where it's read", () => {
    mergeStatus = conflicts();
    const view = render(<Harness />);
    const top = view.container.querySelector<HTMLElement>("[data-header-row]")!;
    const badge = within(top).getByRole("button", {
      name: "Merge status: Conflicts. Show details",
    });
    expect(badge).toHaveTextContent("Conflicts");
    const parts = [...top.querySelectorAll("button, [data-header-part='badges']")];
    expect(parts.indexOf(badge)).toBeGreaterThan(
      parts.indexOf(top.querySelector("[data-header-part='badges']")!),
    );
    expect(parts.indexOf(badge)).toBeLessThan(
      parts.indexOf(within(top).getByRole("button", { name: /^Read from / })),
    );
    // Measured at both lengths by copies that can't take focus.
    const sizers = view.container.querySelectorAll("[data-header-sizers] [data-header-status]");
    expect([...sizers].map((sizer) => sizer.textContent)).toEqual(["Conflicts", "Conflicts", ""]);
  });

  it("opens the Overview's merge section from another tab", async () => {
    mergeStatus = conflicts();
    const onSelect = vi.fn();
    render(<Harness initial="changes" onSelect={onSelect} />);
    await userEvent.click(screen.getByRole("button", { name: /^Merge status: Conflicts/ }));
    expect(onSelect).toHaveBeenCalledWith("summary");
    await vi.waitFor(() => expect(screen.getByLabelText("Merge section")).toHaveFocus());
  });

  it("shows what a change to the PR came to under the header, on every tab", () => {
    const view = render(<Harness initial="changes" />);
    expect(screen.getByText("What the last change came to")).toBeInTheDocument();
    view.unmount();
    render(<Harness initial="summary" />);
    expect(screen.getByText("What the last change came to")).toBeInTheDocument();
  });

  it("shows nothing beside a pill that already says it all", () => {
    mergeStatus = pullRequestStatusDisplay({ state: "OPEN" });
    const view = render(<Harness />);
    expect(screen.queryByRole("button", { name: /^Merge status/ })).toBeNull();
    expect(view.container.querySelector("[data-header-status]")).toBeNull();
  });
});

describe("the header's freshness mark", () => {
  it("says how old kept data is beside the actions, and that it's refreshing", () => {
    freshness = { updatedAt: Date.now() - 12 * 60_000, refreshing: true, failed: false };
    const view = render(<Harness />);
    const mark = view.container.querySelector<HTMLElement>("[data-pull-request-freshness]")!;
    expect(mark.textContent).toBe("Updated 12 min ago · refreshing");
    expect(mark.closest('[data-header-part="actions"]')).not.toBeNull();
    // A narrow header drops only the word, keeping the age and status in view.
    expect(mark.className).not.toMatch(/\bhidden\b/);
    expect(screen.getByText("Updated").className).toMatch(/\bhidden @lg:inline\b/);
  });

  it("shows no mark once the page's data is this run's", () => {
    const view = render(<Harness />);
    expect(view.container.querySelector("[data-pull-request-freshness]")).toBeNull();
  });
});

describe("the header's stack", () => {
  const prUrl = (number: number) => `https://github.com/acme/app/pull/${number}`;
  const BODY = [
    "## Stacked changes",
    `- [stack/client](${prUrl(99)}) ⬆️ (dependency: merge first)`,
    `  - [**stack/362**](${prUrl(362)}) ← _this PR_`,
    `    - [stack/363](${prUrl(363)})`,
    "",
    "---------",
    "Collects diagnostics.",
  ].join("\n");

  function renderStacked(selectInPlace: (url: string) => boolean) {
    const parse = parseStack(STACK_FORMATS, BODY, {
      host: "github.com",
      repository: "acme/app",
      number: 362,
    });
    expect(parse?.trust).toBe("verified");
    const stack = pullRequestStackView(parse!, { kind: "unavailable" }, selectInPlace);
    return render(
      <MemoryRouter>
        <Tabs value="summary">
          <PullRequestPageHeader state={{ ...state(), stack }} options={{}} />
        </Tabs>
      </MemoryRouter>,
    );
  }

  it("shows the switcher and the depends-on chip where there's room", () => {
    fit.current = SEGMENTS;
    renderStacked(() => true);
    expect(screen.getByRole("button", { name: "Stack: level 1 of 2" }).textContent).toContain(
      "1/2",
    );
    expect(screen.getByRole("button", { name: /^Depends on #99/ })).toBeTruthy();
  });

  it("shows the stack from the list before the PR's details arrive", () => {
    const parse = parseStack(STACK_FORMATS, BODY, {
      host: "github.com",
      repository: "acme/app",
      number: 362,
    });
    const stack = pullRequestStackView(parse!, { kind: "unavailable" }, () => true);
    render(
      <MemoryRouter>
        <PullRequestPagePreviewHeader
          preview={{
            url: prUrl(362),
            repository: "acme/app",
            number: 362,
            title: "Collect diagnostics",
            state: "OPEN",
            isDraft: false,
            author: null,
          }}
          copy={gitProviderCopy("github")}
          options={{}}
          stack={stack}
        />
      </MemoryRouter>,
    );
    expect(screen.getByRole("button", { name: "Stack: level 1 of 2" })).toBeTruthy();
  });

  it("chooses the section the PR opens on while it reads", () => {
    render(
      <MemoryRouter>
        <PullRequestPagePreviewHeader
          preview={{
            url: prUrl(362),
            repository: "acme/app",
            number: 362,
            title: "Collect diagnostics",
            state: "OPEN",
            isDraft: false,
            author: null,
          }}
          copy={gitProviderCopy("github")}
          options={{}}
          section="changes"
        />
      </MemoryRouter>,
    );
    expect(screen.getByRole("tab", { name: /^Changes/ })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: /^Overview/ })).toHaveAttribute(
      "aria-selected",
      "false",
    );
  });

  it.each([
    ["changes", "Changes"],
    ["commits", "Commits"],
  ] as const)(
    "names the section the PR opens on in a narrow header's picker: %s",
    (section, label) => {
      fit.current = MENU;
      render(
        <MemoryRouter>
          <PullRequestPagePreviewHeader
            preview={{
              url: prUrl(362),
              repository: "acme/app",
              number: 362,
              title: "Collect diagnostics",
              state: "OPEN",
              isDraft: false,
              author: null,
            }}
            copy={gitProviderCopy("github")}
            options={{}}
            section={section}
          />
        </MemoryRouter>,
      );
      expect(screen.getByRole("combobox", { name: "Pull request section" })).toHaveTextContent(
        label,
      );
    },
  );

  it("holds the switcher's place while the stack reads, and shows none for an unstacked PR", () => {
    const { rerender } = render(
      <MemoryRouter>
        <Tabs value="summary">
          <PullRequestPageHeader
            state={{ ...state(), stack: null, stackLoading: true }}
            options={{}}
          />
        </Tabs>
      </MemoryRouter>,
    );
    expect(screen.getByRole("status", { name: STACK_LOADING })).toBeTruthy();
    rerender(
      <MemoryRouter>
        <Tabs value="summary">
          <PullRequestPageHeader state={{ ...state(), stack: null }} options={{}} />
        </Tabs>
      </MemoryRouter>,
    );
    expect(screen.queryByRole("status", { name: STACK_LOADING })).toBeNull();
  });

  it("keeps the dependency in the menu, by keyboard, where the narrowest header drops its chip", async () => {
    fit.current = {
      ...MENU,
      name: "number",
      lifecycle: "icon",
      stack: "compact",
      spacing: "tight",
    };
    const selected: string[] = [];
    renderStacked((url) => {
      selected.push(url);
      return true;
    });
    expect(screen.queryByRole("button", { name: /^Depends on/ })).toBeNull();
    const trigger = screen.getByRole("button", { name: "Stack: level 1 of 2" });
    expect(trigger.textContent).not.toContain("1/2");
    trigger.focus();
    await userEvent.keyboard("{Enter}");
    const menu = screen.getByRole("menu");
    expect(within(menu).getByText("Depends on")).toBeTruthy();
    // The menu opens on this PR's row, as a select does; the dependency is a step above.
    await waitFor(() => expect(document.activeElement).toHaveAttribute("aria-current", "page"));
    await userEvent.keyboard("{ArrowUp}");
    expect(document.activeElement?.textContent).toContain("#99");
    await userEvent.keyboard("{Enter}");
    expect(selected).toEqual([prUrl(99)]);
  });
});
