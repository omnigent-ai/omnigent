import { describe, expect, it } from "vitest";

import {
  panelAllowsHost,
  panelDestinations,
  type PanelDestinationFacts,
} from "./panelDestinations";

const facts = (over: Partial<PanelDestinationFacts> = {}): PanelDestinationFacts => ({
  thisMacHostId: "host_mac",
  hosts: [
    { host_id: "host_mac", status: "online" },
    { host_id: "host_box", status: "online" },
    { host_id: "host_old", status: "offline" },
  ],
  sandboxesOffered: true,
  ...over,
});

describe("panelDestinations", () => {
  it("allows this Mac and new sandboxes, and no other host", () => {
    const destinations = panelDestinations(facts());
    expect(destinations).toEqual({
      pending: false,
      thisMac: { hostId: "host_mac", online: true },
      sandboxes: true,
    });
    expect(panelAllowsHost(destinations, "host_mac")).toBe(true);
    expect(panelAllowsHost(destinations, "host_box")).toBe(false);
    expect(panelAllowsHost(destinations, "host_old")).toBe(false);
    expect(panelAllowsHost(destinations, null)).toBe(false);
  });

  it("waits while the shell, the host list or the server hasn't answered", () => {
    for (const over of [
      { thisMacHostId: undefined },
      { hosts: null },
      { sandboxesOffered: null },
    ] satisfies Partial<PanelDestinationFacts>[]) {
      expect(panelDestinations(facts(over)).pending).toBe(true);
    }
    // Still waiting on any answer: no host can be chosen, even this Mac's.
    for (const over of [
      { thisMacHostId: undefined },
      { hosts: null },
      { sandboxesOffered: null },
    ] satisfies Partial<PanelDestinationFacts>[]) {
      expect(panelAllowsHost(panelDestinations(facts(over)), "host_mac")).toBe(false);
    }
  });

  it("offers no host when the shell can't say which is this Mac", () => {
    const destinations = panelDestinations(facts({ thisMacHostId: null }));
    expect(destinations.thisMac).toBeNull();
    for (const host of ["host_mac", "host_box", "host_old"]) {
      expect(panelAllowsHost(destinations, host)).toBe(false);
    }
  });

  it("keeps an offline this Mac, which can be started again", () => {
    const destinations = panelDestinations(
      facts({ hosts: [{ host_id: "host_mac", status: "offline" }] }),
    );
    expect(destinations.thisMac).toEqual({ hostId: "host_mac", online: false });
    expect(panelAllowsHost(destinations, "host_mac")).toBe(true);
  });

  it("has no sandbox when the server doesn't offer one", () => {
    expect(panelDestinations(facts({ sandboxesOffered: false })).sandboxes).toBe(false);
  });
});
