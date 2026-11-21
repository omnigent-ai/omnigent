import { describe, expect, it } from "vitest";

import { THINGY_KIT_HELPERS } from "./templateKit";

const plain = new Function(`${THINGY_KIT_HELPERS}; return plain;`)() as (value: unknown) => string;

describe("plain", () => {
  it("drops Markdown marks from text a session wrote", () => {
    expect(plain("Choose **approve** or `request changes`")).toBe(
      "Choose approve or request changes",
    );
    expect(plain("an _emphasis_ and __strong__ word")).toBe("an emphasis and strong word");
    expect(plain("see [the PR](https://example.com/1)")).toBe("see the PR");
  });

  it("keeps the underscores in snake_case names", () => {
    expect(plain("Ramp smart_reminders_enabled to 10%")).toBe(
      "Ramp smart_reminders_enabled to 10%",
    );
    expect(plain("`dark_mode_v2_enabled` and __init__")).toBe("dark_mode_v2_enabled and init");
  });
});
