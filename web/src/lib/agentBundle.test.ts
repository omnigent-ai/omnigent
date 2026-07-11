import { describe, expect, it } from "vitest";

// We can't use buildAgentBundle directly in jsdom because
// CompressionStream is not available. Instead, test the YAML
// generation logic by importing the module and calling the
// internal config builder. Since it's not exported, we test
// indirectly through buildAgentBundle and inspect the generated
// YAML by mocking the compression + tar layer.

// The module's functions are all private except buildAgentBundle.
// We mock CompressionStream and verify the config.yaml content
// that gets fed to the tar/gzip pipeline.

import type { AgentBundleInput } from "./agentBundle";

// Capture what buildAgentBundle passes to `new File(...)` by
// mocking CompressionStream (not in jsdom) to be a passthrough.
class PassthroughStream {
  readable: ReadableStream;
  writable: WritableStream;
  constructor() {
    let controller: ReadableStreamDefaultController;
    this.readable = new ReadableStream({
      start(c) {
        controller = c;
      },
    });
    this.writable = new WritableStream({
      write(chunk) {
        controller.enqueue(new Uint8Array(chunk));
      },
      close() {
        controller.close();
      },
    });
  }
}

// Install mock before importing buildAgentBundle.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
(globalThis as any).CompressionStream = PassthroughStream;

// Now import the function (it will use our mock CompressionStream).
const { buildAgentBundle, buildBundleFromFiles } = await import("./agentBundle");

/** Parse all tar entries (name → content) from the raw tar bytes in a File. */
async function extractEntries(file: File): Promise<Record<string, string>> {
  const tar = new Uint8Array(await file.arrayBuffer());
  const dec = new TextDecoder();
  const entries: Record<string, string> = {};
  let off = 0;
  while (off + 512 <= tar.length) {
    const name = dec.decode(tar.slice(off, off + 100)).replace(/\0/g, "");
    if (!name) break; // zero block → end of archive
    const size = parseInt(dec.decode(tar.slice(off + 124, off + 135)).replace(/\0/g, ""), 8);
    entries[name] = dec.decode(tar.slice(off + 512, off + 512 + size));
    off += 512 + Math.ceil(size / 512) * 512;
  }
  return entries;
}

/** Build a File with an optional `webkitRelativePath` for folder-pick tests. */
function fakeFile(name: string, content: string, relPath?: string): File {
  const file = new File([content], name, { type: "text/yaml" });
  if (relPath) {
    Object.defineProperty(file, "webkitRelativePath", { value: relPath });
  }
  return file;
}

/** Extract the config.yaml from the raw tar bytes inside the File. */
async function extractConfigYaml(file: File): Promise<string> {
  const buf = await file.arrayBuffer();
  const tar = new Uint8Array(buf);
  // First tar entry: 512-byte header, then content.
  // File size is at offset 124, 12 bytes, octal null-terminated.
  const sizeStr = new TextDecoder().decode(tar.slice(124, 135)).replace(/\0/g, "");
  const size = parseInt(sizeStr, 8);
  return new TextDecoder().decode(tar.slice(512, 512 + size));
}

/** Extract AGENTS.md (second tar entry) from the raw tar bytes. */
async function extractAgentsMd(file: File): Promise<string | null> {
  const buf = await file.arrayBuffer();
  const tar = new Uint8Array(buf);
  const size0Str = new TextDecoder().decode(tar.slice(124, 135)).replace(/\0/g, "");
  const size0 = parseInt(size0Str, 8);
  const blocks0 = Math.ceil(size0 / 512);
  const entry1Start = 512 + blocks0 * 512;
  if (entry1Start + 512 > tar.length) return null;
  const name1 = new TextDecoder()
    .decode(tar.slice(entry1Start, entry1Start + 100))
    .replace(/\0/g, "");
  if (!name1.startsWith("AGENTS.md")) return null;
  const size1Str = new TextDecoder()
    .decode(tar.slice(entry1Start + 124, entry1Start + 135))
    .replace(/\0/g, "");
  const size1 = parseInt(size1Str, 8);
  return new TextDecoder().decode(tar.slice(entry1Start + 512, entry1Start + 512 + size1));
}

describe("buildAgentBundle", () => {
  it("produces a tar.gz file with correct config.yaml for minimal input", async () => {
    const input: AgentBundleInput = {
      name: "test-agent",
      harness: "claude-sdk",
      model: "claude-sonnet-4-20250514",
    };
    const file = await buildAgentBundle(input);
    expect(file.name).toBe("agent.tar.gz");
    expect(file.type).toBe("application/gzip");

    const yaml = await extractConfigYaml(file);
    expect(yaml).toContain("spec_version: 1");
    expect(yaml).toContain("name: test-agent");
    expect(yaml).toContain("model: claude-sonnet-4-20250514");
    expect(yaml).toContain("harness: claude-sdk");
    expect(yaml).toContain("web_search");
    expect(yaml).toContain("web_fetch");
    expect(yaml).not.toContain("instructions:");
    expect(yaml).not.toContain("description:");
  });

  it("includes description when provided", async () => {
    const input: AgentBundleInput = {
      name: "my-agent",
      description: "A helpful assistant",
      harness: "claude-sdk",
      model: "claude-sonnet-4-20250514",
    };
    const yaml = await extractConfigYaml(await buildAgentBundle(input));
    expect(yaml).toContain("description: A helpful assistant");
  });

  it("quotes description with special characters", async () => {
    const input: AgentBundleInput = {
      name: "my-agent",
      description: 'Has: colons and "quotes"',
      harness: "claude-sdk",
      model: "claude-sonnet-4-20250514",
    };
    const yaml = await extractConfigYaml(await buildAgentBundle(input));
    expect(yaml).toContain('description: "Has: colons and \\"quotes\\""');
  });

  it("includes AGENTS.md when instructions are provided", async () => {
    const input: AgentBundleInput = {
      name: "my-agent",
      instructions: "You are a helpful assistant.",
      harness: "claude-sdk",
      model: "claude-sonnet-4-20250514",
    };
    const file = await buildAgentBundle(input);
    const yaml = await extractConfigYaml(file);
    expect(yaml).toContain("instructions: AGENTS.md");

    const md = await extractAgentsMd(file);
    expect(md).toBe("You are a helpful assistant.");
  });

  it("omits AGENTS.md when no instructions", async () => {
    const input: AgentBundleInput = {
      name: "my-agent",
      harness: "claude-sdk",
      model: "claude-sonnet-4-20250514",
    };
    const md = await extractAgentsMd(await buildAgentBundle(input));
    expect(md).toBeNull();
  });

  it("includes inline MCP servers (stdio)", async () => {
    const input: AgentBundleInput = {
      name: "mcp-agent",
      harness: "claude-sdk",
      model: "claude-sonnet-4-20250514",
      mcpServers: [
        {
          name: "github",
          transport: "stdio",
          command: "npx",
          args: ["-y", "@modelcontextprotocol/server-github"],
          env: { GITHUB_TOKEN: "ghp_test" },
        },
      ],
    };
    const yaml = await extractConfigYaml(await buildAgentBundle(input));
    expect(yaml).toContain("  github:");
    expect(yaml).toContain("    type: mcp");
    expect(yaml).toContain("    command: npx");
    expect(yaml).toContain('    args: [-y, "@modelcontextprotocol/server-github"]');
    expect(yaml).toContain("      GITHUB_TOKEN: ghp_test");
  });

  it("includes inline MCP servers (http)", async () => {
    const input: AgentBundleInput = {
      name: "http-agent",
      harness: "claude-sdk",
      model: "claude-sonnet-4-20250514",
      mcpServers: [
        {
          name: "search",
          transport: "http",
          url: "https://mcp.example.com/sse",
          headers: { Authorization: "Bearer tok_123" },
        },
      ],
    };
    const yaml = await extractConfigYaml(await buildAgentBundle(input));
    expect(yaml).toContain("  search:");
    expect(yaml).toContain("    type: mcp");
    expect(yaml).toContain('    url: "https://mcp.example.com/sse"');
    expect(yaml).toContain("      Authorization: Bearer tok_123");
  });

  it("uses different harness and model values", async () => {
    const input: AgentBundleInput = {
      name: "oai-agent",
      harness: "openai-agents",
      model: "gpt-4o",
    };
    const yaml = await extractConfigYaml(await buildAgentBundle(input));
    expect(yaml).toContain("harness: openai-agents");
    expect(yaml).toContain("model: gpt-4o");
  });
});

describe("buildBundleFromFiles", () => {
  it("rejects an empty selection", async () => {
    await expect(buildBundleFromFiles([])).rejects.toThrow(/no files/i);
  });

  it("renames a lone YAML to config.yaml when singleFileAsConfig is set", async () => {
    const file = await buildBundleFromFiles([fakeFile("my-agent.yaml", "spec_version: 1")], {
      singleFileAsConfig: true,
    });
    const entries = await extractEntries(file);
    expect(Object.keys(entries)).toEqual(["config.yaml"]);
    expect(entries["config.yaml"]).toBe("spec_version: 1");
  });

  it("keeps the original file name when singleFileAsConfig is not set", async () => {
    const file = await buildBundleFromFiles([fakeFile("agent.yml", "spec_version: 1")]);
    const entries = await extractEntries(file);
    expect(Object.keys(entries)).toEqual(["agent.yml"]);
  });

  it("strips the top folder from webkitRelativePath so contents sit at the root", async () => {
    const file = await buildBundleFromFiles(
      [
        fakeFile("config.yaml", "spec_version: 1", "my-agent/config.yaml"),
        fakeFile("AGENTS.md", "# Instructions", "my-agent/AGENTS.md"),
        fakeFile("SKILL.md", "skill body", "my-agent/skills/debate/SKILL.md"),
      ],
      { singleFileAsConfig: true },
    );
    const entries = await extractEntries(file);
    expect(Object.keys(entries).sort()).toEqual([
      "AGENTS.md",
      "config.yaml",
      "skills/debate/SKILL.md",
    ]);
    // singleFileAsConfig must not rewrite names for a multi-file folder pick.
    expect(entries["config.yaml"]).toBe("spec_version: 1");
    expect(entries["skills/debate/SKILL.md"]).toBe("skill body");
  });

  it("packs a folder's config.yaml verbatim (no YAML rewriting)", async () => {
    const raw = "spec_version: 1\nname: imported\nexecutor:\n  type: omnigent\n";
    const file = await buildBundleFromFiles(
      [fakeFile("config.yaml", raw, "imported/config.yaml")],
      { singleFileAsConfig: true },
    );
    const entries = await extractEntries(file);
    expect(entries["config.yaml"]).toBe(raw);
  });
});
