import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ServerSelectorV2 } from "./ServerSelectorV2";
import { SIMULATED_BANNER, parseAuthFailureScenario } from "./authFailureFixture";
import { maybeMockSetup } from "./mockSetup";

const base = "mock=1&installed=1&remote=1&managed=https://team.example.test";
const open = (extra: string) => {
  const setup = maybeMockSetup(new URLSearchParams(`${base}&${extra}`));
  if (!setup) throw new Error("mock setup expected");
  render(<ServerSelectorV2 setup={setup} />);
};
const writeText = vi.fn();

beforeEach(() => {
  writeText.mockReset().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
});
afterEach(cleanup);

describe("auth failure fixture", () => {
  it("ignores unknown scenarios and requires the mock", () => {
    expect(parseAuthFailureScenario(new URLSearchParams("authFailure=nope"))).toBeNull();
    expect(parseAuthFailureScenario(new URLSearchParams("authFailure=laptop"))).toBe("laptop");
    expect(maybeMockSetup(new URLSearchParams("authFailure=laptop"))).toBeNull();
    expect(maybeMockSetup(new URLSearchParams(base))?.initialTerminal).toBeUndefined();
  });

  it("is disabled outside a development build", () => {
    vi.stubEnv("DEV", false);
    try {
      expect(parseAuthFailureScenario(new URLSearchParams("authFailure=laptop"))).toBeNull();
      expect(
        maybeMockSetup(new URLSearchParams(`${base}&authFailure=laptop`))?.initialTerminal,
      ).toBeUndefined();
    } finally {
      vi.unstubAllEnvs();
    }
  });

  it("shows the laptop failure red, its next step neutral, and the simulated label", async () => {
    open("authFailure=laptop");
    expect(screen.getByRole("note")).toHaveTextContent(SIMULATED_BANNER);
    const failure = await screen.findByText(
      /didn't complete\. A browser window should have opened/,
      {},
      { timeout: 4000 },
    );
    expect(failure).toHaveClass("text-destructive");
    const action = screen.getByText(/Finish signing in and try again/);
    expect(action).not.toHaveClass("text-destructive");
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Back" })).toBeInTheDocument();
  });

  it("surfaces the Arca sign-in error and copies simulated, redacted diagnostics", async () => {
    open("authFailure=arca-auth");
    expect(
      await screen.findByText(
        "The Arca instance isn't signed in to this server.",
        {},
        { timeout: 4000 },
      ),
    ).toHaveClass("text-destructive");
    expect(screen.queryByText(/timed out/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Copy diagnostics/ }));
    expect(await screen.findByText("Copied")).toBeInTheDocument();
    const text = writeText.mock.calls[0][0] as string;
    expect(text).toContain("Simulated: yes");
    expect(text).toContain("Runner: arca");
    expect(text).toContain("Failure kind: omni-auth");
    expect(text).toContain("Server: https://team.example.test");
    expect(text).not.toContain("OMNIGENT_AUTH_REQUIRED");
  });

  it("shows a pure timeout separately from the sign-in error", async () => {
    open("authFailure=arca-timeout");
    expect(
      await screen.findByText(/Connecting to Arca timed out/, {}, { timeout: 4000 }),
    ).toHaveClass("text-destructive");
    expect(screen.queryByText(/isn't signed in/)).toBeNull();
  });

  it("reports a failed copy when the clipboard refuses", async () => {
    open("authFailure=laptop&clipboard=deny");
    fireEvent.click(
      await screen.findByRole("button", { name: /Copy diagnostics/ }, { timeout: 4000 }),
    );
    expect(await screen.findByText(/Couldn't copy/)).toBeInTheDocument();
    expect(writeText).not.toHaveBeenCalled();
  });

  it("recovers on Retry after a first failure", async () => {
    open("authFailure=laptop-recovery");
    fireEvent.click(await screen.findByRole("button", { name: "Retry" }, { timeout: 4000 }));
    expect(await screen.findByText("Server ready", {}, { timeout: 4000 })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
