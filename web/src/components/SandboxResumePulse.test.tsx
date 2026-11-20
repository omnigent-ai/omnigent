/// <reference types="node" />
import { readFileSync } from "node:fs";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { SandboxResumePulse } from "./SandboxResumePulse";

// Relative to the vitest root (web/), as index.css.test reads it.
const indexCss = readFileSync("src/index.css", "utf8");

afterEach(cleanup);

describe("SandboxResumePulse", () => {
  it("names only keyframes the app's stylesheet defines, so the halo really breathes", () => {
    render(<SandboxResumePulse />);
    const classes = [...screen.getByTestId("sandbox-resume-pulse").querySelectorAll("span")]
      .map((span) => span.className)
      .join(" ");
    const names = [...classes.matchAll(/animate-\[([a-z0-9-]+)_/g)].map((match) => match[1]);
    expect(names).toEqual(["sandbox-resume-breathe"]);
    for (const name of names) expect(indexCss).toContain(`@keyframes ${name} {`);
  });
});
