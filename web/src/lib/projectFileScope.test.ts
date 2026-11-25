import { describe, expect, it } from "vitest";
import { isTempConvId } from "./tempConversationId";
import {
  commentsApiBase,
  parseProjectContentLink,
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

  it("recognizes same-origin project file and ticket-attachment content links", () => {
    expect(parseProjectContentLink("/v1/projects/p1/files/f1/content")).toEqual({
      projectId: "p1",
      fileId: "f1",
      kind: "file",
      contentPath: "/v1/projects/p1/files/f1/content",
    });
    expect(parseProjectContentLink("/v1/projects/p1/ticket-attachments/a1/content")?.kind).toBe(
      "attachment",
    );
    expect(
      parseProjectContentLink("https://example.com/v1/projects/p1/files/f1/content"),
    ).toBeNull();
    expect(parseProjectContentLink("/v1/projects/p1/files/f1")).toBeNull();
    expect(parseProjectContentLink("/home/u/ws/report.md")).toBeNull();
    // A video attachment's marker is the only query allowed, and only on attachments.
    expect(
      parseProjectContentLink("/v1/projects/p1/ticket-attachments/a1/content?type=video"),
    ).toMatchObject({
      kind: "attachment",
      contentPath: "/v1/projects/p1/ticket-attachments/a1/content",
    });
    expect(parseProjectContentLink("/v1/projects/p1/files/f1/content?type=video")).toBeNull();
    expect(parseProjectContentLink("/v1/projects/p1/ticket-attachments/a1/content?x=1")).toBeNull();
  });
});
