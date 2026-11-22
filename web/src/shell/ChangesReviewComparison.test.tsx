import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { useWorkingCopyBaseCommands } from "@/hooks/useWorkingCopyChanges";
import {
  localComparisonView,
  type LocalComparisonView,
  type QuickComparison,
} from "@/lib/localComparison";
import type { EffectiveComparison } from "@/lib/workingCopyComparison";
import { readWorkingCopyBaseIntent } from "@/lib/workingCopyBasePreference";
import { setWorkingCopyComparisonsForTests } from "@/lib/workingCopyComparisons";
import { installFakeComparisonOwner } from "@/test/workingCopyComparisons";
import { ComparisonPicker } from "./ChangesReviewComparison";

afterEach(() => {
  setWorkingCopyComparisonsForTests(null);
  window.localStorage.clear();
});

function quickChoice(
  id: QuickComparison["id"],
  label: string,
  overrides: Partial<QuickComparison> = {},
): QuickComparison {
  return {
    id,
    label,
    description: `${label} description`,
    active: false,
    unavailableReason: null,
    choose: vi.fn(),
    ...overrides,
  };
}

function quickChoices(active: QuickComparison["id"] | null = "compare-branch-point") {
  return [
    quickChoice("compare-head", "Uncommitted changes", { active: active === "compare-head" }),
    quickChoice("compare-branch-point", "All branch changes", {
      active: active === "compare-branch-point",
    }),
    quickChoice("compare-last-commit", "Last commit", { active: active === "compare-last-commit" }),
  ];
}

function makeView(overrides: Partial<LocalComparisonView> = {}): LocalComparisonView {
  return {
    trigger: {
      name: "All branch changes",
      base: "origin/main",
      head: "Working Copy",
      title: "t",
      invalid: false,
    },
    quickChoices: quickChoices(),
    presets: [
      {
        id: "branch-point",
        label: "origin/main (merge base)",
        selected: true,
        unavailableReason: null,
        choose: vi.fn(),
      },
      { id: "head", label: "HEAD", selected: false, unavailableReason: null, choose: vi.fn() },
    ],
    reset: { disabled: true, run: vi.fn() },
    revisions: { editable: true, custom: null, fieldError: null, compare: vi.fn() },
    choiceKey: "branch-point",
    revisionsNote: null,
    fallback: null,
    state: { kind: "ready", status: null },
    refresh: vi.fn(),
    retry: vi.fn(),
    showLastCommit: vi.fn(),
    compareAgain: vi.fn(),
    ...overrides,
  };
}

function open(
  view: LocalComparisonView,
  suggestions?: Parameters<typeof ComparisonPicker>[0]["suggestions"],
) {
  const onOpenChange = vi.fn();
  render(
    <ComparisonPicker view={view} open onOpenChange={onOpenChange} suggestions={suggestions} />,
  );
  return { onOpenChange };
}

// A field with suggestions is a combobox.
const base = () => screen.getByRole("combobox", { name: "Base revision" });
const head = () => screen.getByRole("combobox", { name: "Head revision" });

describe("ComparisonPicker with typed revisions", () => {
  it("starts from the comparison on screen and compares what's typed", () => {
    const view = makeView();
    const { onOpenChange } = open(view);
    expect([(base() as HTMLInputElement).value, (head() as HTMLInputElement).value]).toEqual([
      "origin/main",
      "Working Copy",
    ]);
    fireEvent.change(base(), { target: { value: " v1.2 " } });
    fireEvent.change(head(), { target: { value: "feature" } });
    fireEvent.click(screen.getByRole("button", { name: "Compare" }));
    expect(view.revisions.compare).toHaveBeenCalledWith({ base: "v1.2", head: "feature" });
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("refuses text that can't be a revision in its field, before asking", () => {
    const view = makeView();
    open(view);
    fireEvent.change(head(), { target: { value: "--upload-pack=x" } });
    fireEvent.submit(head().closest("form")!);
    expect(screen.getByRole("alert").textContent).toBe("Head can't start with “-”.");
    expect(head().getAttribute("aria-invalid")).toBe("true");
    expect(base().getAttribute("aria-invalid")).toBeNull();
    expect(view.revisions.compare).not.toHaveBeenCalled();
    // Editing the field clears its error.
    fireEvent.change(head(), { target: { value: "feature" } });
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("shows the app's answer about a field while the fields still say what it answered", () => {
    open(
      makeView({
        revisions: {
          editable: true,
          custom: { base: "nope", head: "" },
          fieldError: { field: "base", message: "Base “nope” wasn't found in this repository." },
          compare: vi.fn(),
        },
        reset: { disabled: false, run: vi.fn() },
      }),
    );
    expect((base() as HTMLInputElement).value).toBe("nope");
    expect(screen.getByRole("alert").textContent).toMatch(/wasn't found/);
    expect(base().getAttribute("aria-invalid")).toBe("true");
    fireEvent.change(base(), { target: { value: "main" } });
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("suggests the repository's branches and tags, and says when the list was cut short", () => {
    open(makeView(), { refs: ["main", "v1"], truncated: true, defaultBranchRef: "origin/main" });
    const listId = base().getAttribute("list")!;
    const options = [...document.getElementById(listId)!.querySelectorAll("option")];
    expect(options.map((option) => option.value)).toEqual(["HEAD", "main", "v1"]);
    expect(base().getAttribute("title")).toMatch(/first 1,000/);
  });
});

describe("the picker's draft and the applied comparison", () => {
  const applied = (custom: { base: string; head: string } | null) =>
    makeView({
      trigger: {
        name: custom ? "Custom" : "All branch changes",
        base: custom?.base ?? "origin/main",
        head: custom?.head || "Working Copy",
        title: "t",
        invalid: false,
      },
      presets: makeView().presets.map((preset) => ({
        ...preset,
        selected: !custom && preset.id === "branch-point",
      })),
      reset: { disabled: !custom, run: vi.fn() },
      revisions: { editable: true, custom, fieldError: null, compare: vi.fn() },
    });
  const button = (name: string) => screen.getByRole("button", { name }) as HTMLButtonElement;

  it("shows a newly applied comparison, as after Reset", () => {
    const typed = applied({ base: "v1", head: "feature" });
    const { rerender } = render(<ComparisonPicker view={typed} open onOpenChange={() => {}} />);
    fireEvent.click(button("Reset"));
    expect(typed.reset.run).toHaveBeenCalledOnce();
    rerender(<ComparisonPicker view={applied(null)} open onOpenChange={() => {}} />);
    expect([(base() as HTMLInputElement).value, (head() as HTMLInputElement).value]).toEqual([
      "origin/main",
      "Working Copy",
    ]);
  });

  it("lets Reset discard a draft while the default is already applied", () => {
    const automatic = applied(null);
    render(<ComparisonPicker view={automatic} open onOpenChange={() => {}} />);
    expect(button("Reset").disabled).toBe(true);
    fireEvent.change(base(), { target: { value: "v1" } });
    expect(button("Reset").disabled).toBe(false);
    fireEvent.click(button("Reset"));
    expect((base() as HTMLInputElement).value).toBe("origin/main");
    expect(automatic.reset.run).not.toHaveBeenCalled();
  });

  it("disables Compare while the fields say what's already applied", () => {
    render(
      <ComparisonPicker
        view={applied({ base: "v1", head: "feature" })}
        open
        onOpenChange={() => {}}
      />,
    );
    expect(button("Compare").disabled).toBe(true);
    fireEvent.change(head(), { target: { value: " feature " } });
    expect(button("Compare").disabled).toBe(true);
    fireEvent.change(head(), { target: { value: "main" } });
    expect(button("Compare").disabled).toBe(false);
  });
});

describe("ComparisonPicker without typed revisions", () => {
  it("keeps the presets and the read-only head, and says why", () => {
    open(
      makeView({
        revisions: { editable: false, custom: null, fieldError: null, compare: vi.fn() },
        revisionsNote:
          "Comparing other branches, tags and commits needs a newer version of the desktop app.",
      }),
    );
    expect(base().tagName).toBe("BUTTON");
    const readOnlyHead = screen.getByRole("textbox", { name: "Head revision" });
    expect((readOnlyHead as HTMLInputElement).readOnly).toBe(true);
    expect(screen.getByTestId("changes-review-revisions-note").textContent).toMatch(
      /newer version/,
    );
  });

  it("doesn't repeat below a reason a quick choice already gives", () => {
    const reason =
      "Comparing other branches, tags and commits needs a newer version of the desktop app.";
    open(
      makeView({
        quickChoices: quickChoices().map((entry) =>
          entry.id === "compare-last-commit" ? { ...entry, unavailableReason: reason } : entry,
        ),
        revisions: { editable: false, custom: null, fieldError: null, compare: vi.fn() },
        revisionsNote: reason,
      }),
    );
    expect(screen.getByText(reason)).toBeTruthy();
    expect(screen.queryByTestId("changes-review-revisions-note")).toBeNull();
  });
});

describe("the trigger", () => {
  it("names the comparison in plain words, with the refs in its tooltip", () => {
    open(
      makeView({
        trigger: { ...makeView().trigger, title: "origin/main compared with Working Copy" },
      }),
    );
    const trigger = screen.getByTestId("changes-review-comparison");
    expect(trigger.textContent).toBe("All branch changes");
    expect(trigger.getAttribute("title")).toBe("origin/main compared with Working Copy");
  });

  it("shows base … head when the host didn't say what it compares with", () => {
    open(
      makeView({
        trigger: { name: null, base: null, head: "Working Copy", title: "t", invalid: false },
      }),
    );
    expect(screen.getByTestId("changes-review-comparison").textContent).toBe("Working Copy");
  });
});

describe("the quick choices", () => {
  const choice = (name: RegExp) => screen.getByRole("button", { name });
  const layouts = {
    typed: {},
    preset: { revisions: { editable: false, custom: null, fieldError: null, compare: vi.fn() } },
  } satisfies Record<string, Partial<LocalComparisonView>>;

  it.each(Object.entries(layouts))(
    "sit in a Compare group above the manual fields, marked by the comparison on screen (%s)",
    (_layout, overrides) => {
      open(makeView(overrides));
      const group = screen.getByRole("group", { name: "Compare" });
      const buttons = [...group.querySelectorAll("button")];
      expect(buttons.map((button) => button.getAttribute("aria-pressed"))).toEqual([
        "false",
        "true",
        "false",
      ]);
      expect(choice(/^All branch changes$/)).toHaveAccessibleDescription(
        "All branch changes description",
      );
      // Opening lands on the marked choice, not the Base field.
      expect(document.activeElement).toBe(choice(/^All branch changes$/));
      expect(base()).toBeTruthy();
    },
  );

  it("opens on the first that can run when nothing is marked, and somewhere usable when none can", () => {
    const { unmount } = render(
      <ComparisonPicker
        view={makeView({
          quickChoices: quickChoices(null).map((entry, index) =>
            index === 0 ? { ...entry, unavailableReason: "Not Git." } : entry,
          ),
        })}
        open
        onOpenChange={() => {}}
      />,
    );
    expect(document.activeElement).toBe(choice(/^All branch changes$/));
    unmount();
    open(
      makeView({
        quickChoices: quickChoices(null).map((entry) => ({ ...entry, unavailableReason: "No." })),
      }),
    );
    const content = screen.getByRole("dialog", { name: "Compared revisions" });
    expect(content.contains(document.activeElement)).toBe(true);
  });

  it("runs the marked choice once and closes, by click and by Enter and Space", async () => {
    const user = userEvent.setup();
    const view = makeView();
    const { onOpenChange } = open(view);
    const marked = view.quickChoices[1]!;
    await user.click(choice(/^All branch changes$/));
    expect(marked.choose).toHaveBeenCalledOnce();
    expect(onOpenChange).toHaveBeenLastCalledWith(false);
    choice(/^All branch changes$/).focus();
    await user.keyboard(" ");
    expect(marked.choose).toHaveBeenCalledTimes(2);
    await user.keyboard("{Enter}");
    expect(marked.choose).toHaveBeenCalledTimes(3);
  });

  it("moves with Tab, and the arrows choose nothing", async () => {
    const user = userEvent.setup();
    const view = makeView();
    open(view);
    expect(document.activeElement).toBe(choice(/^All branch changes$/));
    await user.keyboard("{ArrowDown}{ArrowUp}");
    expect(document.activeElement).toBe(choice(/^All branch changes$/));
    await user.tab();
    expect(document.activeElement).toBe(choice(/^Last commit$/));
    for (const entry of view.quickChoices) expect(entry.choose).not.toHaveBeenCalled();
  });

  it("keeps a choice that can't run focusable with its reason, and runs nothing", async () => {
    const user = userEvent.setup();
    const view = makeView({
      quickChoices: quickChoices().map((entry) =>
        entry.id === "compare-last-commit"
          ? { ...entry, unavailableReason: "Update the desktop app to compare revisions." }
          : entry,
      ),
    });
    const { onOpenChange } = open(view);
    const unavailable = choice(/^Last commit$/);
    expect(unavailable.getAttribute("aria-disabled")).toBe("true");
    expect(unavailable).toHaveAccessibleDescription("Update the desktop app to compare revisions.");
    await user.click(unavailable);
    act(() => unavailable.focus());
    await user.keyboard(" {Enter}");
    expect(document.activeElement).toBe(unavailable);
    expect(view.quickChoices[2]!.choose).not.toHaveBeenCalled();
    expect(onOpenChange).not.toHaveBeenCalled();
  });
});

describe("the quick choices with the shared commands", () => {
  const FALLBACK: EffectiveComparison = {
    base: "head",
    note: "Showing uncommitted changes: Comparing took too long.",
    override: false,
    retry: true,
  };
  const BRANCH_POINT: EffectiveComparison = {
    base: "branch-point",
    comparison: {
      id: "cmp_1",
      ref: "origin/main",
      baseOid: "b".repeat(40),
      headOid: "h".repeat(40),
      refOid: "r".repeat(40),
      entries: [],
      unrepresentableNames: 0,
      listedAt: 1,
    },
    status: null,
  };

  function Harness({
    comparison,
    onOpenChange,
  }: {
    comparison: EffectiveComparison;
    onOpenChange: (open: boolean) => void;
  }) {
    const commands = useWorkingCopyBaseCommands("conv_1", { headIsKnown: true });
    const view = localComparisonView({
      comparison,
      headIsKnown: true,
      revisionSupport: { kind: "available" },
      custom: null,
      intent: readWorkingCopyBaseIntent(),
      commands,
      permitted: true,
      readable: true,
      actions: {
        refetchHead: vi.fn(),
        retryBranchPoint: vi.fn(),
        showLastCommit: vi.fn(),
        compareAgain: vi.fn(),
        compareRevisions: vi.fn(),
      },
    });
    return <ComparisonPicker view={view} open onOpenChange={onOpenChange} />;
  }

  function renderHarness(comparison: EffectiveComparison) {
    const fake = installFakeComparisonOwner();
    const chose = vi.spyOn(fake.owner, "chose");
    const onOpenChange = vi.fn();
    const queryClient = new QueryClient();
    const ui = (shown: EffectiveComparison) => (
      <QueryClientProvider client={queryClient}>
        <Harness comparison={shown} onOpenChange={onOpenChange} />
      </QueryClientProvider>
    );
    const rendered = render(ui(comparison));
    return {
      chose,
      onOpenChange,
      rerender: (shown: EffectiveComparison) => rendered.rerender(ui(shown)),
    };
  }

  it.each(["click", "Space"] as const)(
    "pins Uncommitted when the marked fallback is chosen by %s, and keeps it when the branch point returns",
    async (how) => {
      const user = userEvent.setup();
      const { chose, onOpenChange, rerender } = renderHarness(FALLBACK);
      expect(readWorkingCopyBaseIntent()).toBe("automatic");
      const uncommitted = screen.getByRole("button", { name: "Uncommitted changes" });
      expect(uncommitted.getAttribute("aria-pressed")).toBe("true");
      if (how === "click") await user.click(uncommitted);
      else await user.keyboard(" ");
      expect(readWorkingCopyBaseIntent()).toBe("head");
      expect(chose).toHaveBeenCalledOnce();
      expect(chose).toHaveBeenCalledWith("conv_1");
      expect(onOpenChange).toHaveBeenLastCalledWith(false);
      rerender(BRANCH_POINT);
      expect(readWorkingCopyBaseIntent()).toBe("head");
    },
  );

  it("pins All branch changes when the marked branch point Automatic chose is chosen", async () => {
    const user = userEvent.setup();
    const { chose } = renderHarness(BRANCH_POINT);
    const branch = screen.getByRole("button", { name: "All branch changes" });
    expect(branch.getAttribute("aria-pressed")).toBe("true");
    await user.click(branch);
    expect(readWorkingCopyBaseIntent()).toBe("branch-point");
    expect(chose).toHaveBeenCalledOnce();
  });
});

describe("the picker and the branch's commits", () => {
  it("only picks base and head: the commits are the review's Commits tab, not listed here", () => {
    render(<ComparisonPicker view={makeView()} open onOpenChange={vi.fn()} />);
    const popover = screen.getByRole("dialog", { name: "Compared revisions" });
    expect(popover.querySelector("[data-testid='changes-review-commits']")).toBeNull();
    expect(screen.queryByRole("combobox", { name: "Filter commits" })).toBeNull();
    expect(screen.getByRole("combobox", { name: "Base revision" })).toBeTruthy();
  });
});
