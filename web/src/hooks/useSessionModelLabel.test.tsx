import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getOmnigentServerIdentity } from "@/lib/host";
import { getCurrentUserId, resolveIdentity } from "@/lib/identity";
import {
  getSessionModelLabelCacheKey,
  readSessionModelLabelCache,
  writeSessionModelLabelCache,
  type SessionModelLabelScope,
} from "@/lib/sessionModelLabelCache";
import type { NativeModelOption } from "@/lib/types";
import { SESSION_MODEL_LABEL_WAIT_MS, useSessionModelLabel } from "./useSessionModelLabel";

vi.mock("@/lib/host", () => ({ getOmnigentServerIdentity: vi.fn() }));
vi.mock("@/lib/identity", () => ({ getCurrentUserId: vi.fn(), resolveIdentity: vi.fn() }));

const scope: SessionModelLabelScope = {
  sessionId: "session-a",
  hostId: "host-a",
  agentId: "agent-a",
  harness: "claude-native",
};
const model = "provider/model-a";
const catalog: NativeModelOption[] = [
  { id: "alias-a", model, displayName: "Team model (large context)" },
];
interface Props {
  scope: SessionModelLabelScope;
  model: string | null;
  options: readonly NativeModelOption[];
  expectsCatalog: boolean;
  confirmed: boolean;
  hostOptions?: readonly NativeModelOption[];
}
const defaults: Props = { scope, model, options: [], expectsCatalog: true, confirmed: true };
const loading = { label: null, loading: true, unavailable: false };
const named = { label: catalog[0].displayName, loading: false, unavailable: false };

function renderLabel(overrides: Partial<Props> = {}) {
  return renderHook(
    (props: Props) =>
      useSessionModelLabel(
        props.scope,
        props.model,
        props.options,
        props.expectsCatalog,
        props.confirmed,
        props.hostOptions,
      ),
    { initialProps: { ...defaults, ...overrides } },
  );
}
function cacheKey() {
  return getSessionModelLabelCacheKey(scope, model)!;
}

beforeEach(() => {
  localStorage.clear();
  vi.useFakeTimers();
  vi.mocked(getOmnigentServerIdentity).mockReturnValue("server-a");
  vi.mocked(getCurrentUserId).mockReturnValue("user-a");
  vi.mocked(resolveIdentity).mockResolvedValue("user-a");
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
  localStorage.clear();
});

describe("useSessionModelLabel", () => {
  it.each(["alias-a", model])("uses an exact host id or wire-model match: %s", (reported) => {
    const { result } = renderLabel({ model: reported, hostOptions: catalog });
    expect(result.current).toEqual(named);
    expect(vi.getTimerCount()).toBe(0);
  });

  it.each(["codex-native", "codex"])(
    "names an offline %s session's Unity Catalog model from the host catalog",
    (harness) => {
      const codexScope = { ...scope, harness };
      const reported = "system.ai.gpt-5-6-sol";
      const codexProps = { ...defaults, scope: codexScope, model: reported };
      const key = getSessionModelLabelCacheKey(codexScope, reported);
      expect(readSessionModelLabelCache(key)).toBeNull();

      // An offline runner supplies no session catalog; the host probe arrives later.
      const { result, rerender } = renderLabel(codexProps);
      expect(result.current).toEqual(loading);
      rerender({
        ...codexProps,
        hostOptions: [{ id: "gpt-5.6-sol", model: "gpt-5.6-sol", displayName: "GPT-5.6-Sol" }],
      });
      const expected = { label: "GPT-5.6-Sol", loading: false, unavailable: false };
      expect.soft(result.current).toEqual(expected);

      act(() => vi.advanceTimersByTime(SESSION_MODEL_LABEL_WAIT_MS));
      expect.soft(result.current).toEqual(expected);
      expect(readSessionModelLabelCache(key)).toBeNull();
    },
  );

  it("prefers an exact Codex host match over an equivalent spelling", () => {
    const { result } = renderLabel({
      scope: { ...scope, harness: "codex-native" },
      model: "system.ai.gpt-5-6-sol",
      hostOptions: [
        { id: "gpt-5.6-sol", displayName: "Codex name" },
        { id: "system.ai.gpt-5-6-sol", displayName: "Exact name" },
      ],
    });
    expect(result.current).toEqual({ label: "Exact name", loading: false, unavailable: false });
  });

  it.each([
    ["codex-native", "system.ai.gpt-5-6-luna"],
    ["codex-native", "system.ai.gpt-5-5-sol"],
    ["claude-native", "system.ai.gpt-5-6-sol"],
  ])("does not borrow a host name for %s reporting %s", (harness, reported) => {
    const { result } = renderLabel({
      scope: { ...scope, harness },
      model: reported,
      hostOptions: [{ id: "gpt-5.6-sol", displayName: "GPT-5.6-Sol" }],
    });
    act(() => vi.advanceTimersByTime(SESSION_MODEL_LABEL_WAIT_MS));
    expect(result.current).toEqual({ label: null, loading: false, unavailable: true });
  });

  it("does not retain a host name after the model changes or the host catalog disappears", () => {
    const { result, rerender } = renderLabel({ hostOptions: catalog });
    expect(result.current).toEqual(named);
    rerender({ ...defaults, model: "another-model", hostOptions: catalog });
    expect(result.current).toEqual(loading);
    rerender({ ...defaults, hostOptions: catalog });
    expect(result.current).toEqual(named);
    rerender(defaults);
    expect(result.current).toEqual(loading);
    act(() => vi.advanceTimersByTime(SESSION_MODEL_LABEL_WAIT_MS));
    expect(result.current).toEqual({ label: null, loading: false, unavailable: true });
    expect(readSessionModelLabelCache(cacheKey())).toBeNull();
  });

  it("uses a host entry's wire model when it has no display name", () => {
    const { result } = renderLabel({
      model: "alias-a",
      hostOptions: [{ id: "alias-a", model }],
    });
    expect(result.current).toEqual({ label: model, loading: false, unavailable: false });
  });

  it("recovers from timeout with a host name without caching it, then prefers the session catalog", () => {
    const { result, rerender } = renderLabel();
    act(() => vi.advanceTimersByTime(SESSION_MODEL_LABEL_WAIT_MS));
    expect(result.current.unavailable).toBe(true);
    const hostOptions = [{ ...catalog[0], displayName: "Host name" }];
    rerender({ ...defaults, hostOptions });
    expect(result.current).toEqual({ label: "Host name", loading: false, unavailable: false });
    expect(readSessionModelLabelCache(cacheKey())).toBeNull();
    rerender({ ...defaults, hostOptions, options: catalog });
    expect(result.current).toEqual(named);
    expect(readSessionModelLabelCache(cacheKey())).toBe(named.label);
    rerender({ ...defaults, hostOptions });
    expect(result.current).toEqual(named);
  });

  it("does not borrow a host name for a different model or override a populated session catalog", () => {
    const hostOptions = [{ ...catalog[0], displayName: "Host name" }];
    const { result, rerender } = renderLabel({ model: `${model}[1m]`, hostOptions });
    expect(result.current).toEqual(loading);
    act(() => vi.advanceTimersByTime(SESSION_MODEL_LABEL_WAIT_MS));
    expect(result.current.unavailable).toBe(true);
    rerender({ ...defaults, hostOptions, options: [{ id: "another-model" }] });
    expect(result.current).toEqual({ label: model, loading: false, unavailable: false });
  });

  it("waits on a cold catalog, then renders and caches its exact display name", () => {
    const { result, rerender } = renderLabel();
    expect(result.current).toEqual(loading);
    rerender({ ...defaults, options: catalog });
    expect(result.current).toEqual(named);
    expect(readSessionModelLabelCache(cacheKey())).toBe(named.label);
  });

  it("renders synchronously from storage after a remount with no catalog", () => {
    const first = renderLabel({ options: catalog });
    first.unmount();
    const second = renderLabel();
    expect(second.result.current).toEqual(named);
    act(() => vi.advanceTimersByTime(0));
    expect(vi.getTimerCount()).toBe(0);
  });

  it("discovers a warm cache as soon as delayed identity resolves, without an external rerender", async () => {
    writeSessionModelLabelCache(cacheKey(), named.label!);
    vi.mocked(getCurrentUserId).mockReturnValue(null);
    let resolve!: (user: string) => void;
    const identity = new Promise<string>((done) => {
      resolve = done;
    });
    vi.mocked(resolveIdentity).mockReturnValue(identity);
    const { result } = renderLabel();
    expect(result.current).toEqual(loading);
    await act(async () => {
      vi.mocked(getCurrentUserId).mockReturnValue("user-a");
      resolve("user-a");
      await identity;
    });
    expect(result.current).toEqual(named);
  });

  it("does not restart a cache-miss deadline when delayed identity becomes known", async () => {
    vi.mocked(getCurrentUserId).mockReturnValue(null);
    let resolve!: (user: string) => void;
    const identity = new Promise<string>((done) => {
      resolve = done;
    });
    vi.mocked(resolveIdentity).mockReturnValue(identity);
    const { result } = renderLabel();
    act(() => vi.advanceTimersByTime(SESSION_MODEL_LABEL_WAIT_MS - 1));
    await act(async () => {
      vi.mocked(getCurrentUserId).mockReturnValue("user-a");
      resolve("user-a");
      await identity;
    });
    expect(result.current).toEqual(loading);
    act(() => vi.advanceTimersByTime(1));
    expect(result.current).toEqual({ label: null, loading: false, unavailable: true });
  });

  it("replaces a cached name with the live catalog's new display name", () => {
    writeSessionModelLabelCache(cacheKey(), "Old advertised name");
    const { result, rerender } = renderLabel();
    expect(result.current.label).toBe("Old advertised name");
    rerender({ ...defaults, options: catalog });
    expect(result.current).toEqual(named);
    rerender(defaults);
    expect(result.current).toEqual(named);
  });

  it.each([
    { options: [{ id: "alias-a", model }] },
    { options: [{ id: "another-model", displayName: "Other model" }] },
  ])(
    "a settled catalog without a matching display name invalidates the cache: %j",
    ({ options }) => {
      writeSessionModelLabelCache(cacheKey(), "Old advertised name");
      const { result, rerender } = renderLabel({ options });
      expect(result.current).toEqual({ label: model, loading: false, unavailable: false });
      expect(readSessionModelLabelCache(cacheKey())).toBeNull();
      rerender(defaults);
      expect(result.current).toEqual(loading);
    },
  );

  it.each(["alias-a", model])("resolves an exact catalog id or wire model: %s", (reported) => {
    const { result } = renderLabel({ model: reported, options: catalog });
    expect(result.current).toEqual(named);
  });

  it("honors a displayName that is itself the raw id without inventing a prettier name", () => {
    const first = renderLabel({ options: [{ ...catalog[0], displayName: model }] });
    expect(first.result.current.label).toBe(model);
    first.unmount();
    expect(renderLabel().result.current).toEqual({
      label: model,
      loading: false,
      unavailable: false,
    });
  });

  it("does not reuse the previous model's name or normalize model families", () => {
    const { result, rerender } = renderLabel({ options: catalog });
    rerender({ ...defaults, model: "provider/model-a[1m]" });
    expect(result.current).toEqual(loading);
  });

  it.each(["sessionId", "hostId", "agentId", "harness"] as const)(
    "does not borrow mounted labels after a %s change",
    (field) => {
      const { result, rerender } = renderLabel({ options: catalog });
      rerender({ ...defaults, scope: { ...scope, [field]: "another" } });
      expect(result.current).toEqual(loading);
    },
  );

  it("does not borrow mounted labels across accounts or servers", () => {
    const { result, rerender } = renderLabel({ options: catalog });
    vi.mocked(getCurrentUserId).mockReturnValue("user-b");
    rerender(defaults);
    expect(result.current).toEqual(loading);
    vi.mocked(getCurrentUserId).mockReturnValue("user-a");
    vi.mocked(getOmnigentServerIdentity).mockReturnValue("server-b");
    rerender(defaults);
    expect(result.current).toEqual(loading);
  });

  it("never persists an unconfirmed seed or request as a session label", () => {
    const { result, rerender } = renderLabel({ options: catalog, confirmed: false });
    expect(result.current).toEqual(named);
    expect(readSessionModelLabelCache(cacheKey())).toBeNull();
    rerender({ ...defaults, confirmed: false });
    expect(result.current).toEqual(loading);
    rerender({ ...defaults, options: catalog });
    expect(readSessionModelLabelCache(cacheKey())).toBe(named.label);
  });

  it("does not wait for an absent model or a non-catalog harness", () => {
    const { result, rerender } = renderLabel({ model: null });
    expect(result.current).toEqual({ label: null, loading: false, unavailable: false });
    rerender({ ...defaults, expectsCatalog: false });
    expect(result.current).toEqual({ label: model, loading: false, unavailable: false });
    expect(vi.getTimerCount()).toBe(0);
  });

  it("bounds the spinner despite rerenders and fresh empty arrays, then recovers late metadata", () => {
    const { result, rerender } = renderLabel();
    act(() => vi.advanceTimersByTime(SESSION_MODEL_LABEL_WAIT_MS - 1));
    rerender({ ...defaults, options: [] });
    expect(result.current).toEqual(loading);
    act(() => vi.advanceTimersByTime(1));
    expect(result.current).toEqual({ label: null, loading: false, unavailable: true });
    rerender({ ...defaults, options: catalog });
    expect(result.current).toEqual(named);
    act(() => vi.advanceTimersByTime(0));
    expect(vi.getTimerCount()).toBe(0);
  });

  it("gives a newly reported model its own loading grace and cleans up on unmount", () => {
    const { result, rerender, unmount } = renderLabel();
    act(() => vi.advanceTimersByTime(SESSION_MODEL_LABEL_WAIT_MS));
    rerender({ ...defaults, model: "another-model" });
    expect(result.current).toEqual(loading);
    act(() => vi.advanceTimersByTime(SESSION_MODEL_LABEL_WAIT_MS - 1));
    expect(result.current).toEqual(loading);
    unmount();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("keeps the mounted live name if storage fails, without retaining invalidated labels", () => {
    writeSessionModelLabelCache(cacheKey(), "Stale name");
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("full");
    });
    vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    const { result, rerender } = renderLabel({ options: catalog });
    rerender(defaults);
    expect(result.current).toEqual(named);
    rerender({ ...defaults, options: [{ id: "other" }] });
    rerender({ ...defaults, options: [] });
    expect(result.current).toEqual(loading);
  });
});
