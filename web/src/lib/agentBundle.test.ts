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
const { buildAgentBundle } = await import("./agentBundle");

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
