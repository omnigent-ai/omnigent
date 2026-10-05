import { describe, expect, it } from "vitest";

import { splitNameTemplate } from "./TemplateNameInput";

describe("splitNameTemplate", () => {
  it.each([
    ["nightly", [{ kind: "text", text: "nightly" }]],
    [
      "Test Automation {{MMM DD}}",
      [
        { kind: "text", text: "Test Automation " },
        { kind: "placeholder", text: "{{MMM DD}}" },
      ],
    ],
    [
      "from {{MMM}} to {{DD}}",
      [
        { kind: "text", text: "from " },
        { kind: "placeholder", text: "{{MMM}}" },
        { kind: "text", text: " to " },
        { kind: "placeholder", text: "{{DD}}" },
      ],
    ],
    ["Deploy \\{{name}} safely", [{ kind: "text", text: "Deploy \\{{name}} safely" }]],
    ["Test {{MMM DD", [{ kind: "text", text: "Test {{MMM DD" }]],
    [
      "{{YYYY}}{{MM}}",
      [
        { kind: "placeholder", text: "{{YYYY}}" },
        { kind: "placeholder", text: "{{MM}}" },
      ],
    ],
    ["{{yyyy}}", [{ kind: "placeholder", text: "{{yyyy}}" }]],
  ])("splits %s", (value, expected) => {
    expect(splitNameTemplate(value)).toEqual(expected);
  });
});
