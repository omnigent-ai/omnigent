import { describe, expect, it } from "vitest";
import type { Host } from "@/hooks/useHosts";
import { findConnectedArcaHost } from "./arcaHost";

describe("findConnectedArcaHost", () => {
  const seeded: Host = {
    host_id: "seeded-arca",
    name: "corey's arca",
    owner: "corey",
    status: "online",
  };
  const remembered: Host = { ...seeded, host_id: "remembered-arca", name: "remote-box" };
  const laptop: Host = { ...seeded, host_id: "laptop", name: "Corey's MacBook" };

  it("prefers the remembered connected Arca over a seeded name", () => {
    expect(findConnectedArcaHost([laptop, seeded, remembered], remembered.host_id)).toBe(
      remembered,
    );
  });

  it.each([null, "missing-arca", remembered.host_id])(
    "falls back to the connected seeded Arca when the remembered id is %s",
    (storedId) => {
      expect(
        findConnectedArcaHost([laptop, { ...remembered, status: "offline" }, seeded], storedId),
      ).toBe(seeded);
    },
  );

  it("excludes offline Arcas, managed hosts, and ordinary connected computers", () => {
    expect(
      findConnectedArcaHost(
        [laptop, { ...seeded, status: "offline" }, { ...remembered, sandbox_provider: "arclet" }],
        remembered.host_id,
      ),
    ).toBeUndefined();
    expect(findConnectedArcaHost([], null)).toBeUndefined();
  });
});
