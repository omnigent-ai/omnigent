import { describe, expect, it } from "vitest";
import { isTempConvId } from "./tempConversationId";
import {
  commentsApiBase,
  parseProjectFileContentPath,
  parseProjectFileScope,
  projectFileScopeId,
} from "./projectFileScope";

describe("project file scope", () => {
  it("round-trips and stays a client-only ID", () => {
    const id = projectFileScopeId("p1", "f1");
    expect(isTempConvId(id)).toBe(true);
    expect(parseProjectFileScope(id)).toEqual({ projectId: "p1", fileId: "f1" });
    expect(parseProjectFileScope("temp:abc")).toBeNull();
    expect(parseProjectFileScope("sess1")).toBeNull();
  });

  it("routes comments to the project file or the session", () => {
    expect(commentsApiBase(projectFileScopeId("p1", "f1"))).toBe("/v1/projects/p1/files/f1");
    expect(commentsApiBase("sess1")).toBe("/v1/sessions/sess1");
  });

  it("recognizes artifact content links", () => {
    expect(parseProjectFileContentPath("/v1/projects/p1/files/f1/content")).toEqual({
      projectId: "p1",
      fileId: "f1",
    });
    expect(parseProjectFileContentPath("https://example.com/v1/projects/p1/files/f1")).toBeNull();
  });
});
