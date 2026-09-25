import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { WorkspaceFileIcon } from "./WorkspaceFileIcon";

afterEach(cleanup);

describe("WorkspaceFileIcon", () => {
  it.each([
    ["report.pdf", "pdf", "coral", "lucide-file-text"],
    ["notes.md", "document", "blue", "lucide-file-text"],
    ["data.csv", "spreadsheet", "lime", "lucide-file-spreadsheet"],
    ["pitch.pptx", "presentation", "lemon", "lucide-presentation"],
    ["source.tar", "archive", "brown", "lucide-file-archive"],
    ["events.jsonl", "json", "purple", "lucide-file-braces"],
    ["app.tsx", "code", "purple", "lucide-file-code-corner"],
    ["photo.webp", "image", "pink", "lucide-file-image"],
    ["demo.mp4", "video", "indigo", "lucide-file-play"],
    ["song.flac", "audio", "indigo", "lucide-file-headphone"],
    ["chat.sqlite3", "database", "purple", "lucide-database"],
    ["brand.woff2", "font", "default", "lucide-file-type"],
    ["LICENSE", "generic", "default", "lucide-file"],
  ])("maps %s to the requested icon and tag color", (path, group, tagColor, iconClass) => {
    const { container } = render(<WorkspaceFileIcon path={path} />);
    const icon = container.querySelector("svg");

    expect(icon).toHaveAttribute("data-file-type", group);
    expect(icon).toHaveAttribute("data-tag-color", tagColor);
    expect(icon).toHaveClass(iconClass);
  });

  it("uses MIME when it is available", () => {
    const { container } = render(<WorkspaceFileIcon path="preview.bin" mimeType="image/png" />);

    expect(container.querySelector("svg")).toHaveAttribute("data-file-type", "image");
  });
});
