import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { authorizeRegistryService, type RegistryService } from "@/lib/mcpRegistry";
import { useRegistrySignIn } from "./useMcpRegistry";

vi.mock("@/lib/mcpRegistry", () => ({
  registryRequest: vi.fn(),
  authorizeRegistryService: vi.fn(),
}));

const tracker: RegistryService = {
  id: "tracker",
  title: "Tracker",
  description: "",
  auth: "oauth",
  connected: false,
  tools: [],
  connect_provider: "mcp-tracker",
};
const wiki: RegistryService = {
  ...tracker,
  id: "wiki",
  title: "Wiki",
  connect_provider: "mcp-wiki",
};

/** A popup that resolves when the test says so and, like the real one, rejects on abort. */
function pendingPopup() {
  const signals: AbortSignal[] = [];
  let resolve!: () => void;
  vi.mocked(authorizeRegistryService).mockImplementation((_service, signal) => {
    signals.push(signal);
    return new Promise<void>((done, reject) => {
      resolve = done;
      signal.addEventListener("abort", () => reject(new Error("Sign-in cancelled.")), {
        once: true,
      });
    });
  });
  return { signals, finish: () => resolve() };
}

describe("useRegistrySignIn", () => {
  beforeEach(() => {
    vi.mocked(authorizeRegistryService).mockReset();
  });
  afterEach(cleanup);

  it("aborts the sign-in in flight when another starts", async () => {
    const popup = pendingPopup();
    const refresh = vi.fn().mockResolvedValue([{ ...wiki, connected: true }]);
    const { result } = renderHook(() => useRegistrySignIn(refresh));

    let first!: Promise<boolean>;
    act(() => {
      first = result.current.signIn(tracker);
    });
    expect(result.current.authorizing).toBe("tracker");
    let second!: Promise<boolean>;
    act(() => {
      second = result.current.signIn(wiki);
    });
    expect(popup.signals[0]!.aborted).toBe(true);
    expect(popup.signals[1]!.aborted).toBe(false);
    expect(result.current.authorizing).toBe("wiki");

    await act(async () => {
      popup.finish();
    });
    await expect(second).resolves.toBe(true);
    await expect(first).resolves.toBe(false);
    // The aborted attempt neither reports an error nor clears the newer state.
    expect(result.current.error).toBeNull();
    expect(result.current.authorizing).toBeNull();
  });

  it("aborts the sign-in when the owner unmounts", () => {
    const popup = pendingPopup();
    const { result, unmount } = renderHook(() => useRegistrySignIn(vi.fn()));
    act(() => {
      void result.current.signIn(tracker);
    });
    unmount();
    expect(popup.signals[0]!.aborted).toBe(true);
  });

  it("separates a catalog read that failed after the popup from a sign-in that did not take", async () => {
    vi.mocked(authorizeRegistryService).mockResolvedValue(undefined);
    const refresh = vi.fn().mockResolvedValue(undefined);
    const { result } = renderHook(() => useRegistrySignIn(refresh));

    let outcome!: Promise<boolean>;
    act(() => {
      outcome = result.current.signIn(tracker);
    });
    await expect(outcome).resolves.toBe(false);
    await waitFor(() =>
      expect(result.current.error).toEqual({
        id: "tracker",
        message: "Couldn’t confirm the connection. Please try again.",
      }),
    );
  });

  it("reports a service the popup finished for but the catalog still shows disconnected", async () => {
    vi.mocked(authorizeRegistryService).mockResolvedValue(undefined);
    const refresh = vi.fn().mockResolvedValue([tracker]);
    const { result } = renderHook(() => useRegistrySignIn(refresh));

    let outcome!: Promise<boolean>;
    act(() => {
      outcome = result.current.signIn(tracker);
    });
    await expect(outcome).resolves.toBe(false);
    await waitFor(() =>
      expect(result.current.error).toEqual({
        id: "tracker",
        message: "Account is not connected. Please try again.",
      }),
    );
    expect(result.current.authorizing).toBeNull();

    act(() => result.current.clearError());
    expect(result.current.error).toBeNull();
  });
});
