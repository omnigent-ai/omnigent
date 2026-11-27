import { describe, expect, it, vi } from "vitest";

import { sidebarNavCommands } from "./sidebarNavCommands";

describe("sidebarNavCommands", () => {
  it("offers each destination only while its nav link is shown", () => {
    const navigate = vi.fn();
    const all = sidebarNavCommands(
      {
        canvas: { enabled: true, to: "/canvas?c=main" },
        usage: { enabled: true, to: "/usage" },
      },
      navigate,
    );
    expect(all.map((command) => command.label)).toEqual(["Go to Canvas", "Go to Usage"]);
    all[0]!.run();
    expect(navigate).toHaveBeenLastCalledWith("/canvas?c=main");
    all[1]!.run();
    expect(navigate).toHaveBeenLastCalledWith("/usage");

    expect(
      sidebarNavCommands(
        {
          canvas: { enabled: false, to: "/canvas" },
          usage: { enabled: false, to: "/usage" },
        },
        navigate,
      ),
    ).toEqual([]);
  });
});
