import { describe, expect, it } from "vitest";

import { computeBuildVersion } from "./buildVersion";

describe("computeBuildVersion", () => {
  it("is deterministic for the same file set", () => {
    const files = ["assets/index-AAAA.js", "assets/index-BBBB.css", "index.html"];
    expect(computeBuildVersion(files)).toBe(computeBuildVersion(files));
  });

  it("is order-independent (sorts internally)", () => {
    expect(computeBuildVersion(["b.js", "a.css", "c.html"])).toBe(
      computeBuildVersion(["c.html", "a.css", "b.js"]),
    );
  });

  it("changes when a JS chunk hash changes", () => {
    expect(computeBuildVersion(["assets/index-AAAA.js", "assets/s-CCCC.css"])).not.toBe(
      computeBuildVersion(["assets/index-DDDD.js", "assets/s-CCCC.css"]),
    );
  });

  it("changes when a CSS chunk hash changes (covers CSS-only deploys)", () => {
    expect(computeBuildVersion(["assets/index-AAAA.js", "assets/s-CCCC.css"])).not.toBe(
      computeBuildVersion(["assets/index-AAAA.js", "assets/s-EEEE.css"]),
    );
  });

  it("returns a short hex string", () => {
    expect(computeBuildVersion(["a.js"])).toMatch(/^[0-9a-f]+$/);
  });
});
