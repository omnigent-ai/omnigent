import genieCodeLogo from "@/assets/genie-code-logo.svg";
import genieCodeLogoSource from "@/assets/genie-code-logo.svg?raw";
import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { ComposerAgentIcon } from "./ComposerAgentIcon";
import { ComposerAgentIcon as NewChatAgentIcon } from "@/shell/NewChatDialog";

afterEach(cleanup);

describe.each([
  ["session composer", ComposerAgentIcon],
  ["new-session composer", NewChatAgentIcon],
] as const)("%s Genie icon", (_surface, Icon) => {
  it.each(["genie", "custom-reviewer"])("shows the fixed-color mark for %s", (name) => {
    const { container } = render(<Icon agent={{ name, harness: "genie" }} />);
    const mark = container.querySelector("img");
    expect(mark).toHaveAttribute("src", genieCodeLogo);
    expect(mark).not.toHaveClass("dark:invert");
  });
});

it("keeps the CLI banner gradient on the Genie composer mark", () => {
  const { container } = render(<ComposerAgentIcon agent={{ name: "genie", harness: "genie" }} />);
  expect(container.querySelector("img")).toHaveAttribute("src", genieCodeLogo);
  const asset = new DOMParser().parseFromString(genieCodeLogoSource, "image/svg+xml");
  const gradient = asset.querySelector("linearGradient");
  expect(gradient?.getAttribute("gradientUnits")).toBe("objectBoundingBox");
  expect(gradient?.getAttribute("x1")).toBe("0");
  expect(gradient?.getAttribute("y1")).toBe("0");
  expect(gradient?.getAttribute("x2")).toBe("1");
  expect(gradient?.getAttribute("y2")).toBe("1");
  expect(
    Array.from(asset.querySelectorAll("stop"), (stop) => [
      stop.getAttribute("offset"),
      stop.getAttribute("stop-color"),
    ]),
  ).toEqual([
    ["0%", "#6188E5"],
    ["25%", "#9C45E7"],
    ["50%", "#CC00E8"],
    ["75%", "#E74B96"],
    ["100%", "#FC6A48"],
  ]);
  expect(asset.querySelector("path")?.getAttribute("fill")).toBe(`url(#${gradient?.id})`);
  expect(genieCodeLogoSource).not.toContain("currentColor");
});
