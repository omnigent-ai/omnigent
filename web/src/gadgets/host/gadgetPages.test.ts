import { describe, expect, it } from "vitest";

import {
  composeGadgetContextPrompt,
  gadgetBoardNewSessionUrl,
  gadgetBoardPath,
  gadgetNewAutomationUrl,
  gadgetNewSessionUrl,
  gadgetSessionUrl,
} from "./gadgetPages";

const APP = { id: "app-1", name: 'Team "board"' };

function parse(path: string): URL {
  return new URL(path, "https://omnigent.example.com");
}

describe("gadget pages", () => {
  it("appends a note naming the gadget and the gadget skill", () => {
    const prompt = composeGadgetContextPrompt("  Summarize the card  ", APP);

    expect(prompt.startsWith("Summarize the card\n\n---\n")).toBe(true);
    expect(prompt).toContain('"Team \\"board\\"" (gadget_id app-1)');
    expect(prompt).toContain("Load the gadget skill and follow it.");
    expect(prompt).toContain("name what's missing, and stop");
    expect(prompt).not.toMatch(/token|Authorization|mcp/i);
  });

  it("opens New session with the prompt as typed", () => {
    const url = parse(gadgetNewSessionUrl("Line one\nLine & two"));

    expect(url.pathname).toBe("/");
    expect(url.searchParams.get("prompt")).toBe("Line one\nLine & two");
  });

  it("opens New session without a prompt parameter for an empty prompt", () => {
    expect(gadgetNewSessionUrl("")).toBe("/");
  });

  it("opens Automations with the create dialog prefilled", () => {
    const url = parse(
      gadgetNewAutomationUrl({ name: "Daily", prompt: "Refresh", schedule: "FREQ=DAILY;BYHOUR=9" }),
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
    expect(gadgetSessionUrl("s_1")).toBe("/c/s_1");
  });

  it("opens New session from the gadget, marked with the gadget", () => {
    const url = parse(gadgetBoardNewSessionUrl("app/1", "Refresh"));

    expect(url.pathname).toBe("/");
    expect(url.searchParams.get("prompt")).toBe("Refresh");
    expect(url.searchParams.get("gadget_start")).toBe("app/1");
    expect(url.searchParams.has("return_to")).toBe(false);
  });

  it("encodes the gadget in its page path", () => {
    expect(gadgetBoardPath("app/1")).toBe("/gadget/app%2F1");
  });
});
