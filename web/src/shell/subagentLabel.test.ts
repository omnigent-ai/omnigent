import { describe, expect, it } from "vitest";

import type { ChildSessionInfo } from "@/hooks/useChildSessions";

import { childPrimaryLabel, titleHandle } from "./subagentLabel";

const HANDLE = "deploy-profile-page";
const SUMMARY = "Deploy PR41 via web ops";

function child(overrides: Partial<ChildSessionInfo> = {}): ChildSessionInfo {
  return {
    id: "child-1",
    title: `worker:${HANDLE}`,
    task_summary: SUMMARY,
    tool: "worker",
    session_name: HANDLE,
    labels: {},
    current_task_status: "completed",
    last_task_error: null,
    busy: false,
    last_message_preview: null,
    pending_elicitations_count: 0,
    routed_model: null,
    ...overrides,
  };
}

describe("titleHandle", () => {
  it("returns the suffix after the tool prefix", () => {
    expect(titleHandle("worker:deploy-profile-page")).toBe("deploy-profile-page");
    expect(titleHandle("ui:claude:my-agent")).toBe("my-agent");
    expect(titleHandle("claude_code:check:account")).toBe("check:account");
  });

  it("is null without a suffix", () => {
    expect(titleHandle("plain")).toBeNull();
    expect(titleHandle("worker:")).toBeNull();
    expect(titleHandle(null)).toBeNull();
  });
});

describe("childPrimaryLabel", () => {
  it("leads with the summary and follows it with the handle", () => {
    expect(childPrimaryLabel(child())).toBe(`${SUMMARY} · ${HANDLE}`);
  });

  it("keeps the handle alone until a summary is generated", () => {
    expect(childPrimaryLabel(child({ task_summary: null }))).toBe(HANDLE);
  });

  it("takes the handle from the title suffix when session_name is missing", () => {
    expect(childPrimaryLabel(child({ session_name: null }))).toBe(`${SUMMARY} · ${HANDLE}`);
  });

  it("shows the summary alone when the row carries no handle", () => {
    expect(childPrimaryLabel(child({ session_name: null, title: null }))).toBe(SUMMARY);
  });

  it("does not repeat a handle that equals the summary", () => {
    expect(childPrimaryLabel(child({ task_summary: HANDLE }))).toBe(HANDLE);
  });

  it("keeps the handle when the summary merely contains it", () => {
    const partial = child({ task_summary: "Capitalize headings", session_name: "api" });
    expect(childPrimaryLabel(partial)).toBe("Capitalize headings · api");
    expect(childPrimaryLabel(child({ task_summary: `Deploy via ${HANDLE}` }))).toBe(
      `Deploy via ${HANDLE} · ${HANDLE}`,
    );
  });

  it("treats whitespace-only fields as missing", () => {
    expect(childPrimaryLabel(child({ task_summary: "   " }))).toBe(HANDLE);
    expect(childPrimaryLabel(child({ session_name: " ", title: " " }))).toBe(SUMMARY);
  });

  it("uses the caller's fallback for a row with no name at all", () => {
    const nameless = child({ task_summary: null, session_name: null, title: null, tool: null });
    expect(childPrimaryLabel(nameless)).toBe("child-1");
    expect(childPrimaryLabel(nameless, "Sub-agent")).toBe("Sub-agent");
  });

  it("labels a Devin sub-agent by its resolved tool like the other native wrappers", () => {
    const devin = child({
      title: "devin-native-ui-subagent:690d786b",
      session_name: "690d786b",
      tool: "Write alpha.txt",
      task_summary: null,
      labels: { "omnigent.wrapper": "devin-native-ui-subagent" },
    });
    expect(childPrimaryLabel(devin)).toBe("Write alpha.txt");
  });

  it("labels a native sub-agent by its title when the resolved tool is blank", () => {
    const native = child({
      title: "general-purpose:a09d1dd1d8dbc0151",
      session_name: "a09d1dd1d8dbc0151",
      tool: "  ",
      labels: { "omnigent.wrapper": "claude-code-native-ui-subagent" },
    });
    expect(childPrimaryLabel(native)).toBe("general-purpose:a09d1dd1d8dbc0151");
  });

  it("strips the user-added sentinel before taking the handle from the title", () => {
    const added = child({ title: "ui:claude:my-agent", session_name: null, task_summary: null });
    expect(childPrimaryLabel(added)).toBe("my-agent");
  });

  it("never leaks the user-added sentinel when the title has no handle", () => {
    const added = child({ title: "ui:my-agent", session_name: null, task_summary: null });
    expect(childPrimaryLabel(added)).toBe("my-agent");
  });

  it("labels native sub-agents by their resolved tool, not the correlation id", () => {
    const native = child({
      title: "general-purpose:a09d1dd1d8dbc0151",
      session_name: "a09d1dd1d8dbc0151",
      tool: "Investigate flaky auth test",
      labels: { "omnigent.wrapper": "claude-code-native-ui-subagent" },
    });
    expect(childPrimaryLabel(native)).toBe("Investigate flaky auth test");
  });
});
