import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useHosts, type Host } from "@/hooks/useHosts";
import {
  ONBOARDING_RUNNER_GRACE_MS,
  useOnboardingRunnerHost,
} from "@/hooks/useOnboardingRunnerHost";
import { clearImportReviewRequest, requestImportReview } from "@/lib/importReviewState";
import type * as nativeBridge from "@/lib/nativeBridge";
import { resetOnboardingRunnerForTests } from "@/lib/nativeBridge";

const authenticatedFetchMock = vi.hoisted(() => vi.fn());
const features = vi.hoisted(() => ({ import_review: true }));
vi.mock("@/lib/identity", () => ({ authenticatedFetch: authenticatedFetchMock }));
vi.mock("@/lib/CapabilitiesContext", () => ({ useServerInfo: () => ({ features }) }));
vi.mock("@/lib/nativeBridge", async (importOriginal) => ({
  ...(await importOriginal<typeof nativeBridge>()),
  isIOSShell: () => false,
}));

// The desktop shell's onboarding handoff: answers once, then reports nothing.
const shellTake = vi.hoisted(() => vi.fn<() => Promise<"local" | "remote" | null>>());
function installShell(
  runner: "local" | "remote" | null,
  identity: { cliInstalled: boolean; hostId: string | null } | null = {
    cliInstalled: true,
    hostId: "laptop",
  },
) {
  shellTake.mockReset().mockResolvedValueOnce(runner).mockResolvedValue(null);
  (window as unknown as Record<string, unknown>).omnigentDesktop = {
    kind: "electron",
    takeOnboardingRunner: () => shellTake(),
    getHostIdentity: () => Promise.resolve(identity),
  };
}

import { ImportReviewGate, ReviewImportsPanel } from "./HostImportReview";

function host(id: string, overrides: Partial<Host> = {}): Host {
  return {
    host_id: id,
    name: `${id}-machine`,
    owner: "me",
    status: "online",
    configured_harnesses: { "claude-native": true },
    ...overrides,
  };
}

/** Serve hosts plus per-host skill names; MCP inventories are empty. */
function serve(
  hosts: Host[],
  skillsByHost: Record<string, string[]>,
  { absentSkillCalls = 0 }: { absentSkillCalls?: number } = {},
) {
  let absent = absentSkillCalls;
  authenticatedFetchMock.mockImplementation(async (url: string) => {
    const parsed = new URL(url, "http://test");
    if (parsed.pathname === "/v1/hosts") return Response.json({ hosts });
    if (parsed.pathname === "/v1/skills") {
      if (absent > 0) {
        absent -= 1;
        return Response.json({ detail: "host not connected" }, { status: 409 });
      }
      const names = skillsByHost[parsed.searchParams.get("host_id") ?? ""] ?? [];
      return Response.json({ skills: names.map((name) => ({ name, description: "" })) });
    }
    if (parsed.pathname.endsWith("/mcp-servers")) return Response.json({ mcp_servers: [] });
    throw new Error(`unexpected request ${url}`);
  });
}

function renderWithClient(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return { client, ...render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>) };
}

function skillRequestsFor(hostId: string) {
  return authenticatedFetchMock.mock.calls.filter(([url]) =>
    String(url).startsWith(`/v1/skills?host_id=${hostId}&`),
  );
}

beforeEach(() => {
  authenticatedFetchMock.mockReset();
  window.localStorage.clear();
  clearImportReviewRequest();
  resetOnboardingRunnerForTests();
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
  features.import_review = true;
});

const TITLE = "Your imports are ready";

/** Waits long enough for a host's inventory to load and open the modal. */
async function expectStaysClosed() {
  await new Promise((resolve) => {
    setTimeout(resolve, 100);
  });
  expect(screen.queryByText(TITLE)).toBeNull();
}

async function expectStaysOpenWith(text: string) {
  await new Promise((resolve) => {
    setTimeout(resolve, 100);
  });
  expect(screen.getByText(text)).toBeTruthy();
}

const CLOSE_METHODS: [string, () => void][] = [
  ["the X button", () => fireEvent.click(screen.getByRole("button", { name: "Close" }))],
  ["Confirm", () => fireEvent.click(screen.getByRole("button", { name: "Confirm" }))],
  ["Escape", () => fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" })],
  [
    "an overlay click",
    () => {
      const overlay = document.querySelector('[data-slot="dialog-overlay"]');
      if (!overlay) throw new Error("no dialog overlay");
      // Radix dismisses on the click that follows an outside pointerdown.
      fireEvent.pointerDown(overlay);
      fireEvent.click(overlay);
    },
  ],
];

afterEach(cleanup);

/** The new-session picker's read of the same onboarding handoff. */
function HostPickerProbe() {
  const { data: hosts } = useHosts();
  const { hostId } = useOnboardingRunnerHost(hosts);
  return <span data-testid="picker-host">{hostId ?? "none"}</span>;
}

describe("ImportReviewGate", () => {
  it("never opens on its own for an unreviewed online host", async () => {
    serve([host("laptop"), host("box")], { laptop: ["from-laptop"], box: ["from-box"] });
    renderWithClient(<ImportReviewGate />);

    await expectStaysClosed();
    expect(authenticatedFetchMock).not.toHaveBeenCalled();
  });

  it("stays closed when the desktop shell has no onboarding handoff", async () => {
    installShell(null);
    serve([host("laptop")], { laptop: ["from-laptop"] });
    renderWithClient(<ImportReviewGate />);

    await waitFor(() => expect(shellTake).toHaveBeenCalledOnce());
    await expectStaysClosed();
  });

  it.each([
    ["local", "laptop"],
    ["remote", "arca"],
  ] as const)("opens for the %s host onboarding set up", async (runner, hostId) => {
    installShell(runner);
    serve([host("laptop"), host("arca")], { laptop: ["from-laptop"], arca: ["from-arca"] });
    renderWithClient(<ImportReviewGate />);

    expect(await screen.findByText(`/from-${hostId}`)).toBeTruthy();
    expect(screen.getByText(new RegExp(`on ${hostId}-machine\\.`))).toBeTruthy();
  });

  it("shares the handoff with the new-session picker", async () => {
    installShell("remote");
    serve([host("laptop"), host("arca")], { arca: ["from-arca"] });
    renderWithClient(
      <>
        <ImportReviewGate />
        <HostPickerProbe />
      </>,
    );

    expect(await screen.findByText("/from-arca")).toBeTruthy();
    await waitFor(() => expect(screen.getByTestId("picker-host").textContent).toBe("arca"));
    expect(shellTake).toHaveBeenCalledOnce();
  });

  it("leaves the handoff untouched while the import_review feature is off", async () => {
    features.import_review = false;
    installShell("local");
    serve([host("laptop")], { laptop: ["from-laptop"] });
    renderWithClient(
      <>
        <ImportReviewGate />
        <HostPickerProbe />
      </>,
    );

    await waitFor(() => expect(screen.getByTestId("picker-host").textContent).toBe("laptop"));
    await expectStaysClosed();
    expect(skillRequestsFor("laptop")).toHaveLength(0);
  });

  it("waits past the host picker's grace period for a slow remote host", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      installShell("remote");
      serve([host("laptop")], { arca: ["from-arca"] });
      const { client } = renderWithClient(
        <>
          <ImportReviewGate />
          <HostPickerProbe />
        </>,
      );
      expect(await screen.findByText("Connecting to Arca…")).toBeTruthy();

      // A cold Arca boot outlasts the picker's grace period.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(ONBOARDING_RUNNER_GRACE_MS + 1_000);
      });
      expect(screen.getByText("Connecting to Arca…")).toBeTruthy();

      serve([host("laptop"), host("arca")], { arca: ["from-arca"] });
      await client.invalidateQueries({ queryKey: ["hosts"] });
      expect(await screen.findByText("/from-arca")).toBeTruthy();
      // The picker's own preselect still gave up.
      expect(screen.getByTestId("picker-host").textContent).toBe("none");
    } finally {
      vi.useRealTimers();
    }
  });

  it("shows nothing when several hosts could be the remote runner", async () => {
    installShell("remote");
    serve([host("laptop"), host("arca"), host("box")], { arca: ["from-arca"], box: ["from-box"] });
    renderWithClient(<ImportReviewGate />);

    await waitFor(() => expect(shellTake).toHaveBeenCalledOnce());
    await expectStaysClosed();
    expect(screen.queryByText("Connecting to Arca…")).toBeNull();
    expect(skillRequestsFor("arca")).toHaveLength(0);
    expect(skillRequestsFor("box")).toHaveLength(0);
  });

  it.each(["local", "remote"] as const)(
    "shows nothing for %s when the shell can't report this machine",
    async (runner) => {
      installShell(runner, null);
      serve([host("laptop")], { laptop: ["from-laptop"] });
      renderWithClient(<ImportReviewGate />);

      await waitFor(() => expect(shellTake).toHaveBeenCalledOnce());
      await expectStaysClosed();
      expect(skillRequestsFor("laptop")).toHaveLength(0);
    },
  );

  it("waits for this machine to come online after a local onboarding", async () => {
    installShell("local");
    serve([host("laptop", { status: "offline" })], { laptop: ["from-laptop"] });
    const { client } = renderWithClient(<ImportReviewGate />);
    expect(await screen.findByText("Connecting to laptop-machine…")).toBeTruthy();

    serve([host("laptop")], { laptop: ["from-laptop"] });
    await client.invalidateQueries({ queryKey: ["hosts"] });
    expect(await screen.findByText("/from-laptop")).toBeTruthy();
  });

  it("keeps an identified remote host when another host comes online", async () => {
    installShell("remote");
    serve([host("laptop"), host("arca")], { arca: ["from-arca"] });
    const { client } = renderWithClient(<ImportReviewGate />);
    expect(await screen.findByText("/from-arca")).toBeTruthy();

    serve([host("laptop"), host("arca"), host("box")], { arca: ["from-arca"], box: ["from-box"] });
    await client.invalidateQueries({ queryKey: ["hosts"] });
    await expectStaysOpenWith("/from-arca");
  });

  it("closes a waiting remote review once a second candidate appears", async () => {
    installShell("remote");
    serve([host("laptop")], {});
    const { client } = renderWithClient(<ImportReviewGate />);
    expect(await screen.findByText("Connecting to Arca…")).toBeTruthy();

    serve([host("laptop"), host("arca"), host("box")], { arca: ["from-arca"] });
    await client.invalidateQueries({ queryKey: ["hosts"] });
    await waitFor(() => expect(screen.queryByText(TITLE)).toBeNull());
    await expectStaysClosed();
  });

  it.each(CLOSE_METHODS)("closes on %s and stays closed", async (_name, close) => {
    installShell("local");
    serve([host("laptop"), host("box")], { laptop: ["from-laptop"], box: ["from-box"] });
    renderWithClient(<ImportReviewGate />);

    expect(await screen.findByText("/from-laptop")).toBeTruthy();
    close();

    await waitFor(() => expect(screen.queryByText(TITLE)).toBeNull());
    await expectStaysClosed();
    expect(window.localStorage.getItem("omnigent:imports-reviewed:laptop")).not.toBeNull();
    expect(skillRequestsFor("box")).toHaveLength(0);
  });
});

describe("ImportReviewGate with a requested host", () => {
  it("waits for an offline target without showing another host's imports", async () => {
    serve([host("target", { status: "offline" }), host("other")], { target: ["t"], other: ["o"] });
    requestImportReview({ hostId: "target", runner: "remote" });
    renderWithClient(<ImportReviewGate />);

    // A listed host is named rather than described by its runner.
    expect(await screen.findByText("Connecting to target-machine…")).toBeTruthy();
    await waitFor(() => expect(authenticatedFetchMock).toHaveBeenCalled());
    expect(screen.queryByText("/o")).toBeNull();
    expect(screen.queryByText(/is offline/)).toBeNull();
    expect(skillRequestsFor("other")).toHaveLength(0);
  });

  it("shows the target's imports once it connects", async () => {
    // Not registered yet (Arca's tunnel lags its daemon), then online.
    serve([host("other")], { target: ["t"], other: ["o"] });
    requestImportReview({ hostId: "target", runner: "remote" });
    const { client } = renderWithClient(<ImportReviewGate />);
    expect(await screen.findByText("Connecting to Arca…")).toBeTruthy();

    serve([host("other"), host("target")], { target: ["t"], other: ["o"] });
    await client.invalidateQueries({ queryKey: ["hosts"] });

    expect(await screen.findByText("/t")).toBeTruthy();
    expect(screen.getByText(/Found in your harnesses on target-machine\./)).toBeTruthy();
    expect(screen.queryByText("/o")).toBeNull();
  });

  it("treats 409 as still connecting, not an error", async () => {
    serve([host("target")], { target: ["t"] }, { absentSkillCalls: 1 });
    requestImportReview({ hostId: "target" });
    renderWithClient(<ImportReviewGate />);

    expect(await screen.findByText("Checking your harnesses…")).toBeTruthy();
    await waitFor(() => expect(skillRequestsFor("target")).toHaveLength(1));
    expect(screen.queryByText(/Couldn't read/)).toBeNull();
    expect(await screen.findByText("/t", {}, { timeout: 5_000 })).toBeTruthy();
    expect(screen.queryByText(/Couldn't read/)).toBeNull();
  });

  it.each([
    ["local", "Connecting this Mac…"],
    [undefined, "Connecting…"],
  ] as const)("describes an unlisted %s runner while it connects", async (runner, copy) => {
    serve([host("other")], { other: ["o"] });
    requestImportReview({ hostId: "target", runner });
    renderWithClient(<ImportReviewGate />);

    expect(await screen.findByText(copy)).toBeTruthy();
    await waitFor(() => expect(authenticatedFetchMock).toHaveBeenCalled());
    expect(screen.queryByText("/o")).toBeNull();
  });

  it("names a single targeted host", async () => {
    serve([host("target")], { target: ["t"] });
    requestImportReview({ hostId: "target" });
    renderWithClient(<ImportReviewGate />);

    expect(await screen.findByText(/Found in your harnesses on target-machine\./)).toBeTruthy();
  });

  it("shows a reviewed target and clears the request on dismiss", async () => {
    window.localStorage.setItem("omnigent:imports-reviewed:target", "x");
    serve([host("target"), host("other")], { target: ["t"], other: ["o"] });
    requestImportReview({ hostId: "target" });
    renderWithClient(<ImportReviewGate />);

    expect(await screen.findByText("/t")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    // Closing never falls through to another unreviewed host.
    await waitFor(() => expect(screen.queryByText(TITLE)).toBeNull());
    await expectStaysClosed();
    expect(skillRequestsFor("other")).toHaveLength(0);
  });
});

describe("ReviewImportsPanel", () => {
  it("reopens the modal for a reviewed host", async () => {
    window.localStorage.setItem("omnigent:imports-reviewed:a", "x");
    serve([host("a")], { a: ["review"] });
    renderWithClient(<ReviewImportsPanel />);

    fireEvent.click(await screen.findByRole("button", { name: "Review imports on a-machine" }));
    expect(await screen.findByText("/review")).toBeTruthy();
  });

  it("explains when no machine is online", async () => {
    serve([host("a", { status: "offline" })], {});
    renderWithClient(<ReviewImportsPanel />);
    expect(await screen.findByText(/None of your machines are online/)).toBeTruthy();
  });
});
