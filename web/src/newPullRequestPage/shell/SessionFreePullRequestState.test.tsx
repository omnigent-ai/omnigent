import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";

import { OpenSessionPageProvider } from "@/shell/OpenSessionPageContext";

import {
  SESSION_FREE_PULL_REQUEST_TITLE,
  SessionFreePullRequestState,
} from "./SessionFreePullRequestState";

it("says the PR isn't linked here, with no link when no session linked it", () => {
  render(<SessionFreePullRequestState linkingSession={null} />);
  expect(screen.getByRole("status")).toHaveTextContent(SESSION_FREE_PULL_REQUEST_TITLE);
  expect(screen.queryByRole("button")).toBeNull();
  expect(screen.queryByRole("alert")).toBeNull();
});

it("opens the session that linked the PR", async () => {
  const openSessionPage = vi.fn();
  render(
    <OpenSessionPageProvider value={openSessionPage}>
      <SessionFreePullRequestState linkingSession={{ id: "conv_auth", name: "auth-fix" }} />
    </OpenSessionPageProvider>,
  );
  expect(screen.getByRole("status")).toHaveTextContent(SESSION_FREE_PULL_REQUEST_TITLE);
  await userEvent.click(screen.getByRole("button", { name: "Open auth-fix" }));
  expect(openSessionPage).toHaveBeenCalledWith("conv_auth");
});
