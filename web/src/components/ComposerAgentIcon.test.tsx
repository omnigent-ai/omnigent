import genieCodeLogo from "@/assets/genie-code-logo.svg";
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
