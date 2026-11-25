// The acting account in a picker, and the notice a retired Mac change leaves its user.

import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const route = vi.hoisted(() => ({
  current: null as null | { target: { host: string }; actor: { login: string } },
}));
vi.mock("@/newPullRequestPage/hooks/usePullRequests", () => ({
  useMacWriteRoute: () => route.current,
}));

import {
  draftOwner,
  recordAbandonedMacWrite,
  resetDesktopPullRequestWritesForTests,
} from "@/lib/desktopPullRequestWrites";
import { MacWriteAbandonedNotice, MacWriteActingAccount } from "./MacWriteNotices";

const PR = "https://github.com/acme/app/pull/42";

afterEach(() => {
  route.current = null;
  resetDesktopPullRequestWritesForTests();
});

describe("the acting account", () => {
  it("names the Mac account where changes go to this Mac, and nothing otherwise", () => {
    const { rerender } = render(<MacWriteActingAccount prUrl={PR} />);
    expect(screen.queryByText(/Changes are made as/)).toBeNull();
    route.current = { target: { host: "github.com" }, actor: { login: "mac-octo" } };
    rerender(<MacWriteActingAccount prUrl={PR} />);
    expect(screen.getByText("Changes are made as @mac-octo")).toBeInTheDocument();
  });
});

describe("a change that may have been made before a switch of accounts", () => {
  it("is shown only to its owner, links to the PR, and can be dismissed", () => {
    render(
      <MacWriteAbandonedNotice
        prUrl={PR}
        operation="pull_request_labels_update"
        subject="labels"
      />,
    );
    expect(screen.queryByRole("status")).toBeNull();
    act(() =>
      recordAbandonedMacWrite({
        owner: "someone-else",
        prUrl: PR,
        operation: "pull_request_labels_update",
      }),
    );
    expect(screen.queryByRole("status")).toBeNull();
    act(() =>
      recordAbandonedMacWrite({
        owner: draftOwner(),
        prUrl: PR,
        operation: "pull_request_labels_update",
      }),
    );
    expect(screen.getByRole("status")).toHaveTextContent(
      "A change to labels may have been made before you switched accounts. Check the pull request on GitHub.",
    );
    expect(screen.getByRole("link", { name: "Open on GitHub" })).toHaveAttribute("href", PR);
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByRole("status")).toBeNull();
  });
});
