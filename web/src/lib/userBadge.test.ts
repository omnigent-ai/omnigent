import { describe, expect, it } from "vitest";

import { userColor, userColorTint, userInitials } from "./userBadge";

describe("userInitials", () => {
  it("takes the first letters of the first two local-part segments", () => {
    expect(userInitials("alice.smith@example.com")).toBe("AS");
    expect(userInitials("corey-zumar@databricks.com")).toBe("CZ");
    expect(userInitials("a_b@example.com")).toBe("AB");
  });

  it("falls back to a single initial for unsegmented local parts", () => {
    expect(userInitials("bob@example.com")).toBe("B");
  });

  it("handles identities without an @", () => {
    expect(userInitials("local")).toBe("L");
  });
});

describe("userColor / userColorTint", () => {
  it("is deterministic per identity", () => {
    // The same email must render the same circle everywhere it appears
    // (presence stack, message badges) and across reloads.
    expect(userColor("alice@example.com")).toBe(userColor("alice@example.com"));
  });

  it("resolves to one of the theme's design tokens", () => {
    // The whole point of the palette: avatar colors are the app's own
    // chart/brand tokens (theme-aware via CSS vars), never an invented
    // hue. A raw hsl()/hex here means the palette mapping regressed.
    expect(userColor("alice@example.com")).toMatch(/^var\(--(chart-[1-4]|brand-accent)\)$/);
  });

  it("differs between identities that hash to different slots", () => {
    // alice → slot 3, bob → slot 0 under FNV-1a mod 5 — a pair chosen
    // to occupy distinct slots, proving the hash actually spreads
    // users across the palette rather than collapsing to one token.
    expect(userColor("alice@example.com")).not.toBe(userColor("bob@example.com"));
  });

  it("tint mixes the same token at reduced strength", () => {
    const solid = userColor("alice@example.com");
    // Same token ties the bubble tint to the author's avatar circle;
    // the color-mix keeps message text readable over it.
    expect(userColorTint("alice@example.com")).toBe(
      `color-mix(in oklab, ${solid} 15%, transparent)`,
    );
  });
});
