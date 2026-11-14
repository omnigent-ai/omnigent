import { describe, expect, it, vi } from "vitest";
import { authenticatedFetch } from "@/lib/identity";
import { fetchHarnessCatalog } from "./agentLabels";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));

describe("fetchHarnessCatalog", () => {
  it("collects the rows that declare workspace_file_mentions", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValue(
      Response.json({
        data: [
          {
            id: "claude-sdk",
            label: "Claude SDK",
            capabilities: { workspace_file_mentions: true },
          },
          { id: "codex", label: "Codex", capabilities: { workspace_file_mentions: null } },
          { id: "pi", label: "Pi" },
        ],
      }),
    );

    const catalog = await fetchHarnessCatalog();

    expect(catalog.fileMentionHarnesses).toEqual(new Set(["claude-sdk"]));
  });
});
