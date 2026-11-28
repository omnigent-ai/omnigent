import { renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import * as DesktopFeatureFlagHooks from "@/hooks/useDesktopFeatureFlag";

import { desktopFeatureFlagModule } from "./desktopFeatureFlagMock";

describe("desktopFeatureFlagModule", () => {
  it("stands in for every hook the real module exports", () => {
    const stub = desktopFeatureFlagModule((_name, fallback) => ({
      value: fallback,
      resolved: true,
    }));
    expect(Object.keys(stub).sort()).toEqual(Object.keys(DesktopFeatureFlagHooks).sort());
  });

  it("answers both hooks from one read, and reads nothing for a disabled reader", () => {
    const reads: string[] = [];
    const stub = desktopFeatureFlagModule((name, fallback, mode) => {
      reads.push(`${name}:${mode}`);
      return { value: name === "on" ? true : fallback, resolved: name !== "pending" };
    });
    const { result } = renderHook(() => ({
      state: stub.useDesktopFeatureFlag("on", false),
      reading: stub.useDesktopFeatureFlagReading("on", false, "resolved"),
      pending: stub.useDesktopFeatureFlagReading("pending", false),
      disabled: stub.useDesktopFeatureFlagReading("off", false, "value", false),
    }));
    expect(result.current).toEqual({
      state: { value: true, resolved: true },
      reading: { value: true, resolved: true, source: "shell" },
      pending: { value: false, resolved: false, source: "pending" },
      disabled: { value: false, resolved: false, source: "pending" },
    });
    expect(reads).toEqual(["on:value", "on:resolved", "pending:value"]);
  });
});
