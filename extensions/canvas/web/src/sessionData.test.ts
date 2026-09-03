import { describe, expect, it, vi } from "vitest";
import type { ExtensionContext } from "@omnigent/extension-sdk";
import { loadSessions } from "./sessionData";

function context(capabilities: string[]): ExtensionContext {
  return {
    capabilities,
    sessions: { listAll: vi.fn(async () => []) },
  } as unknown as ExtensionContext;
}

describe("loadSessions", () => {
  it("requires the sessions capability", async () => {
    await expect(loadSessions(context([]))).rejects.toThrow("sessions.read");
  });

  it("loads all bounded pages through the SDK", async () => {
    const extensionContext = context(["sessions.listPage"]);
    await expect(loadSessions(extensionContext)).resolves.toEqual([]);
    expect(extensionContext.sessions.listAll).toHaveBeenCalledWith({
      pageLimit: 25,
    });
  });
});
