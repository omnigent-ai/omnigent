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

import type { AgentBundleInput, BundleFile } from "./agentBundle";

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

// Now import the functions (they will use our mock CompressionStream).
const { buildAgentBundle, buildBundleFromFiles, stripCommonPrefix, pendingAgentDisplay } =
  await import("./agentBundle");

/** Decode a null-padded tar header field to its leading text. */
function tarField(bytes: Uint8Array): string {
  return new TextDecoder().decode(bytes).split("\u0000")[0];
}

/** List every tar entry name (walks the 512-byte header chain). */
async function tarEntryNames(file: File): Promise<string[]> {
  const tar = new Uint8Array(await file.arrayBuffer());
  const names: string[] = [];
  let offset = 0;
  while (offset + 512 <= tar.length) {
    const name = tarField(tar.slice(offset, offset + 100));
    if (name === "") break; // end-of-archive zero block
    const size = parseInt(tarField(tar.slice(offset + 124, offset + 135)), 8);
    names.push(name);
    offset += 512 + Math.ceil(size / 512) * 512;
  }
  return names;
}

function bf(path: string, content = "x"): BundleFile {
  return { path, bytes: new TextEncoder().encode(content) };
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

describe("stripCommonPrefix", () => {
  it("removes a shared top-level directory", () => {
    const out = stripCommonPrefix([bf("my-agent/config.yaml"), bf("my-agent/AGENTS.md")]);
    expect(out.map((f) => f.path)).toEqual(["config.yaml", "AGENTS.md"]);
  });

  it("leaves paths unchanged when there is no common directory", () => {
    const out = stripCommonPrefix([bf("config.yaml"), bf("AGENTS.md")]);
    expect(out.map((f) => f.path)).toEqual(["config.yaml", "AGENTS.md"]);
  });

  it("does not strip when only a filename shares the segment", () => {
    // A single top-level file must not be treated as a directory prefix.
    const out = stripCommonPrefix([bf("config.yaml")]);
    expect(out.map((f) => f.path)).toEqual(["config.yaml"]);
  });
});

describe("buildBundleFromFiles", () => {
  it("strips the common prefix and drops junk", async () => {
    const file = await buildBundleFromFiles([
      bf("my-agent/config.yaml", "spec_version: 1\nname: x"),
      bf("my-agent/AGENTS.md"),
      bf("my-agent/.git/HEAD"),
      bf("my-agent/node_modules/pkg/index.js"),
      bf("my-agent/.DS_Store"),
    ]);
    const names = await tarEntryNames(file);
    expect(names.sort()).toEqual(["AGENTS.md", "config.yaml"]);
  });

  it("accepts a single top-level YAML (no config.yaml)", async () => {
    const file = await buildBundleFromFiles([bf("agent.yaml", "spec_version: 1\nname: x")]);
    expect(await tarEntryNames(file)).toEqual(["agent.yaml"]);
  });

  it("throws when no top-level YAML exists", async () => {
    await expect(buildBundleFromFiles([bf("my-agent/AGENTS.md")])).rejects.toThrow(/config\.yaml/);
  });

  it("throws when multiple top-level YAMLs exist", async () => {
    await expect(buildBundleFromFiles([bf("a.yaml"), bf("b.yaml")])).rejects.toThrow(
      /top-level YAML/,
    );
  });

  it("rejects path traversal", async () => {
    await expect(buildBundleFromFiles([bf("config.yaml"), bf("../escape.txt")])).rejects.toThrow(
      /Unsafe file path/,
    );
  });

  it("rejects an empty selection", async () => {
    await expect(buildBundleFromFiles([bf(".DS_Store")])).rejects.toThrow(/No files/);
  });
});

describe("pendingAgentDisplay", () => {
  it("reads form fields", () => {
    expect(
      pendingAgentDisplay({
        kind: "form",
        input: { name: "f", description: "d", harness: "claude-sdk", model: "m" },
      }),
    ).toEqual({ name: "f", description: "d", harness: "claude-sdk" });
  });

  it("reads bundle fields", () => {
    const bundle = new File([new Uint8Array()], "agent.tar.gz");
    expect(pendingAgentDisplay({ kind: "bundle", bundle, name: "b" })).toEqual({
      name: "b",
      description: undefined,
      harness: null,
    });
  });

  it("reads github fields", () => {
    expect(pendingAgentDisplay({ kind: "github", sourceUrl: "acme/bot", name: "bot" })).toEqual({
      name: "bot",
      harness: null,
    });
  });
});
