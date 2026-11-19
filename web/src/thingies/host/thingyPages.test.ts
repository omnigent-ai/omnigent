import { describe, expect, it } from "vitest";

import {
  composeThingyContextPrompt,
  thingyBoardNewSessionUrl,
  thingyBoardPath,
  thingyNewAutomationUrl,
  thingyNewSessionUrl,
  thingyOpenPath,
  thingySessionUrl,
} from "./thingyPages";

const APP = { id: "app-1", name: 'Team "board"' };

function parse(path: string): URL {
  return new URL(path, "https://omnigent.example.com");
}

describe("thingy pages", () => {
  it("appends a note naming the thingy and its describe tool", () => {
    const prompt = composeThingyContextPrompt("  Summarize the card  ", APP);

    expect(prompt.startsWith("Summarize the card\n\n---\n")).toBe(true);
    expect(prompt).toContain('"Team \\"board\\"" (thingy_id app-1)');
    expect(prompt).toContain("Call sys_thingy_describe for thingy_id app-1");
    expect(prompt).not.toMatch(/token|Authorization|mcp/i);
  });

  it("opens New session with the prompt as typed", () => {
    const url = parse(thingyNewSessionUrl("Line one\nLine & two"));

    expect(url.pathname).toBe("/");
    expect(url.searchParams.get("prompt")).toBe("Line one\nLine & two");
  });

  it("opens New session without a prompt parameter for an empty prompt", () => {
    expect(thingyNewSessionUrl("")).toBe("/");
  });

  it("opens Automations with the create dialog prefilled", () => {
    const url = parse(
      thingyNewAutomationUrl({ name: "Daily", prompt: "Refresh", schedule: "FREQ=DAILY;BYHOUR=9" }),
    );

    expect(url.pathname).toBe("/tasks");
    expect(Object.fromEntries(url.searchParams)).toMatchObject({
      new: "1",
      name: "Daily",
      prompt: "Refresh",
      schedule: "FREQ=DAILY;BYHOUR=9",
    });
  });

  it("opens a session at its conversation path", () => {
    expect(thingySessionUrl("s_1")).toBe("/c/s_1");
  });

  it("opens New session from the thingy, marked with the thingy", () => {
    const url = parse(thingyBoardNewSessionUrl("app/1", "Refresh"));

    expect(url.pathname).toBe("/");
    expect(url.searchParams.get("prompt")).toBe("Refresh");
    expect(url.searchParams.get("thingy_start")).toBe("app/1");
    expect(url.searchParams.has("return_to")).toBe(false);
  });

  it("encodes the thingy in its page and full screen paths", () => {
    expect(thingyBoardPath("app/1")).toBe("/thingy/app%2F1");
    expect(parse(thingyOpenPath("app/1")).searchParams.get("thingy_open")).toBe("app/1");
  });
});
