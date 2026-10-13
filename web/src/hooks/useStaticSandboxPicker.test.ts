import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { showToast } from "@/components/ui/toast";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { useRemoveStaticSandbox, useStaticSandboxes } from "@/lib/staticSandboxesApi";
import type { StaticSandbox } from "@/lib/staticSandboxes";
import {
  STATIC_CREATE_SLOW_MS,
  staticSandboxWorkspaceFields,
  useStaticSandboxPicker,
  type UseStaticSandboxPickerOptions,
} from "./useStaticSandboxPicker";

vi.mock("@/components/ui/toast", () => ({ showToast: vi.fn() }));
vi.mock("@/lib/CapabilitiesContext", () => ({ useServerInfo: vi.fn() }));
vi.mock("@/lib/staticSandboxesApi", () => ({
  useStaticSandboxes: vi.fn(),
  useRemoveStaticSandbox: vi.fn(),
  wakeStaticSandbox: vi.fn(() => Promise.resolve()),
}));

const toast = vi.mocked(showToast);
const removeMutate = vi.fn();

function vm(overrides: Partial<StaticSandbox> = {}): StaticSandbox {
  return {
    provider: "remote",
    id: "foo",
    status: "running",
    repo: "",
    lastActivityAt: null,
    label: "Checkout",
    ...overrides,
  };
}

function setup(sandboxes: StaticSandbox[], options: Partial<UseStaticSandboxPickerOptions> = {}) {
  vi.mocked(useStaticSandboxes).mockReturnValue({ data: sandboxes } as ReturnType<
    typeof useStaticSandboxes
  >);
  const onSelect = vi.fn();
  const hook = renderHook((props: Partial<UseStaticSandboxPickerOptions>) =>
    useStaticSandboxPicker({
      initialId: null,
      sandboxSelected: false,
      message: "",
      onSelect,
      ...options,
      ...props,
    }),
  );
  return { ...hook, onSelect };
}

beforeEach(() => {
  vi.mocked(useServerInfo).mockReturnValue({ static_sandboxes: true } as ReturnType<
    typeof useServerInfo
  >);
  vi.mocked(useRemoveStaticSandbox).mockReturnValue({
    mutate: removeMutate,
  } as unknown as ReturnType<typeof useRemoveStaticSandbox>);
});

afterEach(() => {
  vi.clearAllMocks();
  vi.useRealTimers();
});

describe("staticSandboxWorkspaceFields", () => {
  it("targets the first repo, else the VM's primary repo", () => {
    expect(staticSandboxWorkspaceFields(["https://g/a#main", "https://g/b"], true)).toEqual({
      workspace: "https://g/a#main",
    });
    expect(staticSandboxWorkspaceFields([], false)).toEqual({});
    expect(staticSandboxWorkspaceFields([], true)).toEqual({ workspace: null, git: null });
  });
});

describe("useStaticSandboxPicker", () => {
  it("is disabled until the server offers static sandboxes", () => {
    vi.mocked(useServerInfo).mockReturnValue("loading");
    const { result } = setup([]);
    expect(result.current.enabled).toBe(false);
    expect(vi.mocked(useStaticSandboxes)).toHaveBeenCalledWith({ enabled: false });
  });

  it("selects after switching to the sandbox target and names the pick", () => {
    const { result, onSelect } = setup([vm()]);
    expect(result.current.selectedLabel).toBeNull();
    act(() => result.current.select("foo"));
    expect(onSelect).toHaveBeenCalledOnce();
    expect(result.current.id).toBe("foo");
    expect(result.current.selectedLabel).toBe("Checkout");
  });

  it("falls back to the id while the list has no entry for it", () => {
    const { result } = setup([], { initialId: "bar" });
    expect(result.current.selectedLabel).toBe("bar");
  });

  it("replaces the managed create fields only while the sandbox target is picked", () => {
    const { result, rerender } = setup([vm()], { initialId: "foo" });
    expect(result.current.createTarget([], false)).toEqual({});
    rerender({ sandboxSelected: true });
    const managed = {
      host_type: "managed",
      workspaces: ["https://g/a"],
      workspace: null,
      git: null,
      sandbox_provider: "kubernetes",
      inference_configuration_revision: "rev",
    };
    const body = JSON.parse(
      JSON.stringify({ ...managed, ...result.current.createTarget(["https://g/a"], true) }),
    );
    expect(body).toEqual({
      host_type: "external",
      sandbox_target: { kind: "static", provider: "remote", sandbox_id: "foo" },
      workspace: "https://g/a",
    });
    expect(
      JSON.parse(JSON.stringify({ ...managed, ...result.current.createTarget([], true) })),
    ).toEqual({
      host_type: "external",
      sandbox_target: { kind: "static", provider: "remote", sandbox_id: "foo" },
      workspace: null,
      git: null,
    });
  });

  it("toasts a paused VM's wake and a slow create until it settles", () => {
    vi.useFakeTimers();
    const { result } = setup([vm({ status: "paused" })], {
      initialId: "foo",
      sandboxSelected: true,
    });
    const end = result.current.beginCreate();
    expect(toast).toHaveBeenCalledWith("Waking Checkout… (~30s), then starting your session on it");
    vi.advanceTimersByTime(STATIC_CREATE_SLOW_MS);
    expect(toast).toHaveBeenLastCalledWith("Preparing Checkout… (up to 2 minutes the first time)");
    end();
    toast.mockClear();
    const endFast = result.current.beginCreate();
    endFast();
    vi.advanceTimersByTime(STATIC_CREATE_SLOW_MS);
    expect(toast).toHaveBeenCalledOnce();
  });

  it("stays quiet for a managed sandbox create", () => {
    vi.useFakeTimers();
    const { result } = setup([vm()], { initialId: "foo" });
    result.current.beginCreate()();
    vi.advanceTimersByTime(STATIC_CREATE_SLOW_MS);
    expect(toast).not.toHaveBeenCalled();
  });

  it("opens a requested rename once the menu closes, without selecting the row", () => {
    const { result, onSelect } = setup([vm()]);
    act(() => {
      result.current.requestRename("foo");
      result.current.selectFromMenu("foo");
    });
    expect(onSelect).not.toHaveBeenCalled();
    expect(result.current.renaming).toBeNull();
    act(() => result.current.onMenuOpenChange(false));
    expect(result.current.renaming?.id).toBe("foo");
    act(() => result.current.closeRename());
    expect(result.current.renaming).toBeNull();
    act(() => result.current.selectFromMenu("foo"));
    expect(result.current.id).toBe("foo");
  });

  it("opens a requested add once the menu closes", () => {
    const { result } = setup([]);
    act(() => result.current.requestAdd());
    act(() => result.current.onMenuOpenChange(true));
    expect(result.current.addOpen).toBe(false);
    act(() => result.current.onMenuOpenChange(false));
    expect(result.current.addOpen).toBe(true);
  });

  it("drops the pick when its entry is removed", () => {
    const { result } = setup([vm({ status: "gone" })], { initialId: "foo" });
    act(() => result.current.remove("foo"));
    expect(removeMutate).toHaveBeenCalledWith({ provider: "remote", sandboxId: "foo" });
    expect(result.current.id).toBeNull();
  });
});
