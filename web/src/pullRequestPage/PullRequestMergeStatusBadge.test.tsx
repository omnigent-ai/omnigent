import { act, cleanup, render, renderHook, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";
import type { PullRequestChecks } from "@/newPullRequestPage/hooks/usePullRequests";
import type { MergeReadinessStatus, MergeReadinessView } from "@/lib/mergeReadiness";
import { pullRequestStatusDisplay } from "@/lib/pullRequestStatusDisplay";

import {
  MERGE_SECTION_ATTRIBUTE,
  PullRequestMergeStatusBadge,
  revealMergeSection,
  steadyHeaderMergeStatus,
  useSteadyHeaderMergeStatus,
} from "./PullRequestMergeStatusBadge";
import { registerPullRequestIntegrations } from "@/lib/pullRequestIntegrations";
import { exampleMergeReadinessSource } from "@/test/pullRequestIntegrations";

// Merge readiness from a stand-in source, as a build that registers one shows it. Registered as
// the file loads, since some displays are built while the tests are collected.
registerPullRequestIntegrations({
  mergeReadiness: exampleMergeReadinessSource(async () => ({ kind: "checking" })),
});

const OPEN = { state: "OPEN" };

function known(
  status: MergeReadinessStatus,
  failingJobs: string[] = [],
): Extract<MergeReadinessView, { kind: "known" }> {
  return {
    kind: "known",
    readiness: { status, reasons: { failingJobs, failingRequirements: [] } },
    headSha: "abc",
  };
}

function badge(
  display: ReturnType<typeof pullRequestStatusDisplay>,
  {
    length = "full",
    checking = false,
    onOpen = vi.fn(),
    checks,
  }: {
    length?: "full" | "short" | "icon";
    checking?: boolean;
    onOpen?: () => void;
    checks?: PullRequestChecks;
  } = {},
) {
  render(
    <TooltipProvider delayDuration={0}>
      <PullRequestMergeStatusBadge
        status={display}
        checking={checking}
        length={length}
        onOpen={onOpen}
        checks={checks}
      />
    </TooltipProvider>,
  );
  return screen.getByRole("button", { name: /^Merge status: / });
}

describe("PullRequestMergeStatusBadge", () => {
  it("shows GitHub's own conflicts in red where merge readiness is off", () => {
    const display = pullRequestStatusDisplay({ state: "OPEN", merge_state_status: "DIRTY" });
    const button = badge(display);
    expect(button).toHaveTextContent("Merge conflicts");
    expect(button).toHaveClass("bg-status-red/10");
    expect(button).toHaveAccessibleDescription("Resolve the merge conflicts with the base branch.");
  });

  it("carries its one-line reason in a tooltip, such as the failing jobs", async () => {
    const display = pullRequestStatusDisplay(OPEN, known("ci_failing", ["unit", "lint"]));
    const button = badge(display);
    await userEvent.hover(button);
    expect(await screen.findByRole("tooltip")).toHaveTextContent(
      "Blocking jobs failing: unit, lint.",
    );
    expect(button).toHaveAccessibleDescription("Blocking jobs failing: unit, lint.");
  });

  it("uses the short label where the header is narrow, the full one staying its name", () => {
    const button = badge(pullRequestStatusDisplay(OPEN, known("policy_failing")), {
      length: "short",
    });
    expect(button).toHaveTextContent(/^Policy failing$/);
    expect(button).toHaveAccessibleName("Merge status: Policy checks failing. Show details");
  });

  it("keeps just its icon where the header is narrowest, its label still its name", () => {
    const button = badge(pullRequestStatusDisplay(OPEN, known("conflicts")), { length: "icon" });
    expect(button).toHaveTextContent(/^$/);
    expect(button.querySelector("svg")).not.toBeNull();
    expect(button).toHaveAccessibleName("Merge status: Conflicts. Show details");
    expect(button).toHaveClass("bg-status-red/10");
  });

  it("names its status in the tooltip as an icon, ahead of any reason", async () => {
    const button = badge(pullRequestStatusDisplay(OPEN, known("conflicts")), { length: "icon" });
    await userEvent.hover(button);
    expect(await screen.findByRole("tooltip")).toHaveTextContent(
      "Conflicts. Resolve the merge conflicts with the base branch.",
    );
  });

  it("names its status alone in the tooltip as an icon when there's no reason", async () => {
    const failing = pullRequestStatusDisplay(OPEN, known("ci_failing"));
    expect(failing.detail).toBeUndefined();
    const button = badge(failing, { length: "icon" });
    // The label is already its name, so nothing more describes it until the tooltip opens.
    expect(button).not.toHaveAttribute("aria-description");
    await userEvent.hover(button);
    expect(await screen.findByRole("tooltip")).toHaveTextContent(/^CI failing$/);
  });

  it("opens its details from a click or the keyboard", async () => {
    const onOpen = vi.fn();
    const button = badge(pullRequestStatusDisplay(OPEN, known("conflicts")), { onOpen });
    await userEvent.click(button);
    act(() => button.focus());
    await userEvent.keyboard("{Enter}");
    expect(onOpen).toHaveBeenCalledTimes(2);
  });

  it("reads as provisional while a new commit is checked", () => {
    const button = badge(pullRequestStatusDisplay(OPEN, known("ready")), { checking: true });
    expect(button).toHaveAttribute("aria-busy", "true");
    expect(button).toHaveClass("opacity-60");
    expect(button).toHaveAccessibleDescription("Checking the latest commit…");
  });
});

describe("steadyHeaderMergeStatus", () => {
  const url = "https://github.com/acme/app/pull/1";
  const conflicts = pullRequestStatusDisplay(OPEN, known("conflicts"));

  it("shows a status that adds to the lifecycle pill", () => {
    expect(steadyHeaderMergeStatus(url, conflicts, null)).toEqual({
      status: conflicts,
      checking: false,
    });
  });

  it("stays quiet where it would repeat the pill, or before anything is known", () => {
    for (const display of [
      pullRequestStatusDisplay(OPEN),
      pullRequestStatusDisplay({ state: "OPEN", is_draft: true }),
      pullRequestStatusDisplay({ state: "MERGED" }, known("conflicts")),
      pullRequestStatusDisplay(null),
      pullRequestStatusDisplay(OPEN, known("open")),
      pullRequestStatusDisplay(OPEN, { kind: "checking" }),
    ]) {
      expect(steadyHeaderMergeStatus(url, display, null)).toBeNull();
    }
  });

  it("keeps the last status for the same PR while its new commit is checked", () => {
    const checking = pullRequestStatusDisplay(OPEN, { kind: "checking" });
    expect(steadyHeaderMergeStatus(url, checking, { prUrl: url, status: conflicts })).toEqual({
      status: conflicts,
      checking: true,
    });
    // Another PR's last status never stands in.
    expect(
      steadyHeaderMergeStatus(url, checking, { prUrl: `${url}0`, status: conflicts }),
    ).toBeNull();
    // A pill-repeating answer replaces it at once.
    expect(
      steadyHeaderMergeStatus(url, pullRequestStatusDisplay(OPEN, known("open")), {
        prUrl: url,
        status: conflicts,
      }),
    ).toBeNull();
  });
});

describe("useSteadyHeaderMergeStatus", () => {
  const url = "https://github.com/acme/app/pull/1";
  const checking = () => pullRequestStatusDisplay(OPEN, { kind: "checking" });
  const steady = (initial: ReturnType<typeof pullRequestStatusDisplay>, prUrl = url) =>
    renderHook(({ display, pr }) => useSteadyHeaderMergeStatus(pr, display), {
      initialProps: { display: initial, pr: prUrl },
    });

  it("keeps the last shown status through a check, then takes the new answer", () => {
    const { result, rerender } = steady(pullRequestStatusDisplay(OPEN, known("conflicts")));
    rerender({ display: checking(), pr: url });
    expect(result.current).toMatchObject({
      status: { key: "merge:conflicts" },
      checking: true,
    });
    rerender({ display: pullRequestStatusDisplay(OPEN, known("ready")), pr: url });
    expect(result.current).toMatchObject({ status: { key: "merge:ready" }, checking: false });
  });

  it("forgets a status once a newer answer shows nothing, so a check can't bring it back", () => {
    const { result, rerender } = steady(pullRequestStatusDisplay(OPEN, known("conflicts")));
    rerender({ display: pullRequestStatusDisplay(OPEN, known("open")), pr: url });
    expect(result.current).toBeNull();
    rerender({ display: checking(), pr: url });
    expect(result.current).toBeNull();
    // Nor does an answer with no status at all, such as one that hasn't loaded.
    rerender({ display: pullRequestStatusDisplay(OPEN, known("conflicts")), pr: url });
    rerender({ display: undefined as never, pr: url });
    rerender({ display: checking(), pr: url });
    expect(result.current).toBeNull();
  });

  it("never lets another PR's status stand in while this one is checked", () => {
    const { result, rerender } = steady(pullRequestStatusDisplay(OPEN, known("conflicts")));
    rerender({ display: checking(), pr: `${url}0` });
    expect(result.current).toBeNull();
    // Back on the first PR, its own answer is still the one from before the switch.
    rerender({ display: checking(), pr: url });
    expect(result.current).toBeNull();
  });
});

describe("revealMergeSection", () => {
  it("focuses the section once the Overview renders it", async () => {
    const root = document.createElement("div");
    document.body.append(root);
    const scrolled = vi.fn();
    revealMergeSection(root);
    const section = document.createElement("div");
    section.setAttribute(MERGE_SECTION_ATTRIBUTE, "");
    section.tabIndex = -1;
    section.scrollIntoView = scrolled;
    root.append(section);
    await vi.waitFor(() => expect(section).toHaveFocus());
    expect(scrolled).toHaveBeenCalledWith({ block: "start" });
    root.remove();
  });
});

describe("a checks status in the header", () => {
  const FAILING: PullRequestChecks = {
    passing: 2,
    failing: 1,
    pending: 0,
    total: 3,
    runs: [
      { name: "build", bucket: "passing", url: null },
      { name: "unit", bucket: "failing", url: "https://ci.example.com/unit" },
      { name: "lint", bucket: "passing", url: null },
    ],
  };

  it.each([
    ["GitHub's Checks failing", pullRequestStatusDisplay({ state: "OPEN", checks: FAILING })],
    ["merge readiness's CI failing", pullRequestStatusDisplay(OPEN, known("ci_failing", ["unit"]))],
  ])("lists the checks for %s, as the table's CI cell does", async (_name, display) => {
    const onOpen = vi.fn();
    const button = badge(display, { onOpen, checks: FAILING });
    expect(button).toHaveAccessibleName(`Merge status: ${display.label}. Show checks`);
    // Its own tooltip gives the reason, so no browser title shows over it.
    expect(button).not.toHaveAttribute("title");
    await userEvent.click(button);
    const list = await screen.findByRole("dialog", { name: "Checks" });
    expect(list).toHaveTextContent("1 failing check · 2 of 3 passed");
    // Failures first, as the table lists them.
    expect(list.textContent!.indexOf("unit")).toBeLessThan(list.textContent!.indexOf("build"));
    expect(onOpen).not.toHaveBeenCalled();
    // Escape closes the list and gives the badge its focus back.
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Checks" })).toBeNull();
    expect(button).toHaveFocus();
  });

  it("opens the merge section for any other status, or where no checks are listed", async () => {
    const onOpen = vi.fn();
    const conflicts = badge(pullRequestStatusDisplay(OPEN, known("conflicts")), {
      onOpen,
      checks: FAILING,
    });
    await userEvent.click(conflicts);
    expect(onOpen).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("dialog", { name: "Checks" })).toBeNull();
  });

  it("opens the merge section for checks it has no list for, or while a new commit is checked", async () => {
    for (const options of [
      { checks: { ...FAILING, runs: [] } },
      { checks: FAILING, checking: true },
    ]) {
      const onOpen = vi.fn();
      const button = badge(pullRequestStatusDisplay(OPEN, known("ci_failing")), {
        onOpen,
        ...options,
      });
      // eslint-disable-next-line no-await-in-loop -- one badge at a time.
      await userEvent.click(button);
      expect(onOpen).toHaveBeenCalledTimes(1);
      expect(screen.queryByRole("dialog", { name: "Checks" })).toBeNull();
      cleanup();
    }
  });
});
