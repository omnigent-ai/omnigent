import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { onTestFinished, describe, expect, it } from "vitest";

import { PullRequestReadPath } from "./PullRequestReadPath";

const HOST = "github.com";
const PR = "https://github.com/example-org/universe/pull/42";
const AUTOMATIC = "Automatic (gh’s active account)";

/**
 * A shell whose choice for github.com changes without the account in use
 * changing: alice stays in use, and no change event is ever sent.
 */
function installShell() {
  let intent: { mode: "automatic" } | { mode: "manual"; login: string } = { mode: "automatic" };
  const choice = () => ({
    ok: true,
    data: {
      intent,
      effective: { login: "alice", generation: 1 },
      accounts: [
        { login: "alice", active: true, state: "success" },
        { login: "bob", active: false, state: "success" },
      ],
    },
  });
  const target = window as unknown as Record<string, unknown>;
  target.omnigentDesktop = {
    kind: "electron",
    codeHostAccounts: {
      get: async () => choice(),
      set: async (_host: string, next: typeof intent) => {
        intent = next;
        return choice();
      },
      onChanged: () => () => {},
    },
  };
  onTestFinished(() => {
    delete target.omnigentDesktop;
  });
}

/** As a session panel shows it. */
function renderReadPath() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <PullRequestReadPath
        read={{ provider: "github", host: HOST, login: "alice" }}
        prUrl={PR}
        hostCanAnswer={false}
      />
    </QueryClientProvider>,
  );
}

async function checkedChoice(): Promise<string | null> {
  await userEvent.click(screen.getByRole("button", { name: /Read as @alice on this Mac/ }));
  const menu = await screen.findByRole("menu");
  const checked = within(menu)
    .getAllByRole("menuitemradio")
    .find((item) => item.getAttribute("aria-checked") === "true");
  await userEvent.keyboard("{Escape}");
  return checked?.textContent ?? null;
}

async function choose(name: string) {
  await userEvent.click(screen.getByRole("button", { name: /Read as @alice on this Mac/ }));
  await userEvent.click(await screen.findByRole("menuitemradio", { name }));
}

describe("PullRequestReadPath", () => {
  it("shows a new choice for the account already in use, in both directions", async () => {
    installShell();
    renderReadPath();
    expect(await checkedChoice()).toBe(AUTOMATIC);

    await choose("@alice");
    await expect.poll(checkedChoice).toBe("@alice");

    await choose(AUTOMATIC);
    await expect.poll(checkedChoice).toBe(AUTOMATIC);
  });
});
