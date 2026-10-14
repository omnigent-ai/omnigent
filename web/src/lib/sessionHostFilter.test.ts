import { afterEach, describe, expect, it, vi } from "vitest";

import type { Host } from "@/hooks/useHosts";

import {
  matchesSessionHostFilter,
  readSessionHostFilter,
  sessionHostFilterOptions,
  writeSessionHostFilter,
} from "./sessionHostFilter";

function host(host_id: string, name: string, extra: Partial<Host> = {}): Host {
  return { host_id, name, owner: "me", status: "online", ...extra };
}

const laptop = host("host_laptop", "laptop");
const desktop = host("host_desktop", "Desktop", { status: "offline" });
const modalA = host("host_modal_a", "sbx-a", { sandbox_provider: "modal" });
const modalB = host("host_modal_b", "sbx-b", { sandbox_provider: "modal" });
const hostsById = new Map([laptop, desktop, modalA, modalB].map((h) => [h.host_id, h] as const));

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("matchesSessionHostFilter", () => {
  it("matches everything on All machines", () => {
    expect(matchesSessionHostFilter({ host_id: null }, "all", hostsById)).toBe(true);
    expect(matchesSessionHostFilter({ host_id: "host_laptop" }, "all", hostsById)).toBe(true);
  });

  it("scopes to one host by id", () => {
    expect(
      matchesSessionHostFilter({ host_id: "host_laptop" }, "host:host_laptop", hostsById),
    ).toBe(true);
    expect(
      matchesSessionHostFilter({ host_id: "host_desktop" }, "host:host_laptop", hostsById),
    ).toBe(false);
    expect(matchesSessionHostFilter({ host_id: null }, "host:host_laptop", hostsById)).toBe(false);
  });

  it("treats sessions without a host as the local machine", () => {
    expect(matchesSessionHostFilter({ host_id: null }, "local", hostsById)).toBe(true);
    expect(matchesSessionHostFilter({}, "local", hostsById)).toBe(true);
    expect(matchesSessionHostFilter({ host_id: "host_laptop" }, "local", hostsById)).toBe(false);
  });

  it("groups every sandbox of a provider under one filter", () => {
    expect(matchesSessionHostFilter({ host_id: "host_modal_a" }, "sandbox:modal", hostsById)).toBe(
      true,
    );
    expect(matchesSessionHostFilter({ host_id: "host_modal_b" }, "sandbox:modal", hostsById)).toBe(
      true,
    );
    expect(
      matchesSessionHostFilter({ host_id: "host_modal_a" }, "host:host_modal_a", hostsById),
    ).toBe(false);
  });
});

describe("sessionHostFilterOptions", () => {
  it("lists the local machine first, then hosts by name, sandboxes grouped", () => {
    const options = sessionHostFilterOptions(
      [{ host_id: null }, { host_id: "host_laptop" }],
      [laptop, desktop, modalA, modalB],
      "all",
    );
    expect(options).toEqual([
      { value: "local", label: "Local machine" },
      { value: "host:host_desktop", label: "Desktop", status: "offline" },
      { value: "host:host_laptop", label: "laptop", status: "online" },
      { value: "sandbox:modal", label: "Modal Sandbox" },
    ]);
  });

  it("omits the local machine when no loaded session ran locally", () => {
    const options = sessionHostFilterOptions([{ host_id: "host_laptop" }], [laptop], "all");
    expect(options.map((o) => o.value)).toEqual(["host:host_laptop"]);
  });

  it("offers hosts seen only on sessions, labelled by id", () => {
    // A shared session can run on another user's host, which /v1/hosts omits.
    const options = sessionHostFilterOptions([{ host_id: "host_theirs" }], [laptop], "all");
    expect(options).toContainEqual({ value: "host:host_theirs", label: "host_theirs" });
  });

  it("keeps a stale selection listed so the viewer can leave it", () => {
    const options = sessionHostFilterOptions([], [laptop], "host:host_gone");
    expect(options).toContainEqual({ value: "host:host_gone", label: "host_gone" });
  });
});

describe("session host filter preference", () => {
  it("defaults to All machines", () => {
    expect(readSessionHostFilter()).toBe("all");
  });

  it("round-trips every filter shape", () => {
    for (const value of ["all", "local", "host:host_laptop", "sandbox:modal"] as const) {
      writeSessionHostFilter(value);
      expect(readSessionHostFilter()).toBe(value);
    }
  });

  it("falls back to All machines for an unknown stored value", () => {
    localStorage.setItem("omnigent:session-host-filter", "laptop");
    expect(readSessionHostFilter()).toBe("all");
    localStorage.setItem("omnigent:session-host-filter", "host:");
    expect(readSessionHostFilter()).toBe("all");
  });

  it("never throws when storage is inaccessible", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota exceeded");
    });
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("access denied");
    });
    expect(() => writeSessionHostFilter("local")).not.toThrow();
    expect(readSessionHostFilter()).toBe("all");
  });
});
