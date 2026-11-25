import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  createDatabricksLaptopFeaturePreference,
  databricksLaptopFeatureEnabled,
  resetDatabricksLaptopForTests,
  useDatabricksLaptop,
} from "./databricksLaptopFeature";

const laptopCheck = vi.hoisted(() => vi.fn<() => Promise<boolean>>());

vi.mock("@/lib/nativeBridge", () => ({ isDatabricksLaptop: () => laptopCheck() }));

describe("databricksLaptopFeatureEnabled", () => {
  it("follows the policy on a confirmed laptop under Automatic", () => {
    expect(
      databricksLaptopFeatureEnabled({ intent: "automatic", automaticPolicy: true, laptop: true }),
    ).toBe(true);
  });

  it("is off under Automatic off a laptop, while the check runs, or with the policy off", () => {
    for (const laptop of [false, null]) {
      expect(
        databricksLaptopFeatureEnabled({ intent: "automatic", automaticPolicy: true, laptop }),
      ).toBe(false);
    }
    expect(
      databricksLaptopFeatureEnabled({ intent: "automatic", automaticPolicy: false, laptop: true }),
    ).toBe(false);
  });

  it("lets On and Off override the policy and the laptop check", () => {
    for (const automaticPolicy of [true, false]) {
      for (const laptop of [true, false, null]) {
        expect(databricksLaptopFeatureEnabled({ intent: "on", automaticPolicy, laptop })).toBe(
          true,
        );
        expect(databricksLaptopFeatureEnabled({ intent: "off", automaticPolicy, laptop })).toBe(
          false,
        );
      }
    }
  });
});

describe("createDatabricksLaptopFeaturePreference", () => {
  const preference = createDatabricksLaptopFeaturePreference("omnigent:test-laptop-feature");

  afterEach(() => window.localStorage.clear());

  it("reads Automatic when nothing, or something unknown, is stored", () => {
    expect(preference.read()).toBe("automatic");
    window.localStorage.setItem(preference.storageKey, "sometimes");
    expect(preference.read()).toBe("automatic");
  });

  it("stores On and Off, and Automatic as nothing", () => {
    preference.write("off");
    expect(window.localStorage.getItem(preference.storageKey)).toBe("off");
    preference.write("on");
    expect(preference.read()).toBe("on");
    preference.write("automatic");
    expect(window.localStorage.getItem(preference.storageKey)).toBeNull();
  });

  it("keeps a mounted intent live across writes, other windows and bulk imports", () => {
    const { result } = renderHook(() => preference.useIntent());
    act(() => preference.write("off"));
    expect(result.current).toBe("off");

    act(() => {
      window.localStorage.setItem(preference.storageKey, "on");
      window.dispatchEvent(new StorageEvent("storage", { key: preference.storageKey }));
    });
    expect(result.current).toBe("on");

    act(() => {
      window.localStorage.removeItem(preference.storageKey);
      preference.notifyChanged();
    });
    expect(result.current).toBe("automatic");
  });

  it("ignores other keys' storage events", () => {
    const listener = vi.fn();
    const unsubscribe = preference.subscribe(listener);
    window.dispatchEvent(new StorageEvent("storage", { key: "omnigent:something-else" }));
    expect(listener).not.toHaveBeenCalled();
    unsubscribe();
  });
});

describe("useDatabricksLaptop", () => {
  beforeEach(() => {
    resetDatabricksLaptopForTests();
    laptopCheck.mockReset();
  });

  it("is pending until the desktop shell answers, then holds its answer", async () => {
    let answer: (laptop: boolean) => void = () => {};
    laptopCheck.mockReturnValue(
      new Promise((resolve) => {
        answer = resolve;
      }),
    );
    const { result } = renderHook(() => useDatabricksLaptop());
    expect(result.current).toBeNull();

    await act(async () => answer(true));
    expect(result.current).toBe(true);
    expect(laptopCheck).toHaveBeenCalledTimes(1);
  });

  it("asks once for every component on the page", async () => {
    laptopCheck.mockResolvedValue(false);
    renderHook(() => useDatabricksLaptop());
    const { result } = renderHook(() => useDatabricksLaptop());
    await waitFor(() => expect(result.current).toBe(false));
    expect(laptopCheck).toHaveBeenCalledTimes(1);
  });

  it("is false when the check fails", async () => {
    laptopCheck.mockRejectedValue(new Error("IPC channel closed"));
    const { result } = renderHook(() => useDatabricksLaptop());
    await waitFor(() => expect(result.current).toBe(false));
  });
});
