/**
 * Client-side agent bundle builder.
 *
 * Produces a minimal `.tar.gz` bundle accepted by the server's
 * multipart `POST /v1/sessions` endpoint. The bundle contains a
 * `config.yaml` and optionally an `AGENTS.md` instructions file.
 */

/** An MCP server to include in the agent bundle. */
export interface MCPServerInput {
  name: string;
  /** "http" for SSE endpoints, "stdio" for local subprocesses. */
  transport: "http" | "stdio";
  /** SSE endpoint URL (http transport only). */
  url?: string;
  /** HTTP headers (http transport only), e.g. {"Authorization": "Bearer ..."} */
  headers?: Record<string, string>;
  /** Executable to spawn (stdio transport only), e.g. "npx". */
  command?: string;
  /** Args for the command (stdio transport only). */
  args?: string[];
  /** Env vars for the subprocess (stdio transport only). */
  env?: Record<string, string>;
}

export interface AgentBundleInput {
  name: string;
  description?: string;
  instructions?: string;
  /** Harness kind, e.g. "claude-sdk", "openai-agents". */
  harness: string;
  /** Model identifier, e.g. "claude-sonnet-4-20250514". Required by the omnigent executor. */
  model: string;
  /** MCP server declarations to include as inline tools entries. */
  mcpServers?: MCPServerInput[];
}

/**
 * Build a `.tar.gz` agent bundle from the given input fields.
 *
 * Uses the pako library (already a transitive dep via codemirror) for
 * gzip compression and a hand-rolled POSIX tar header for the two
 * files. The result is a `File` suitable for `FormData.append`.
 */
export async function buildAgentBundle(input: AgentBundleInput): Promise<File> {
  // Build config.yaml content
  const lines: string[] = ["spec_version: 1", ""];
  lines.push(`name: ${input.name}`);
  if (input.description) {
    lines.push(`description: ${yamlQuote(input.description)}`);
  }
  lines.push("");

  lines.push("executor:");
  lines.push("  type: omnigent");
  lines.push(`  model: ${input.model}`);
  lines.push("  config:");
  lines.push(`    harness: ${input.harness}`);
  lines.push("");

  lines.push("tools:");
  lines.push("  builtins:");
  lines.push("    - web_search");
  lines.push("    - web_fetch");
  // Inline MCP server declarations (parsed by _parse_inline_mcp_servers).
  if (input.mcpServers?.length) {
    for (const mcp of input.mcpServers) {
      lines.push(`  ${mcp.name}:`);
      lines.push("    type: mcp");
      if (mcp.transport === "stdio") {
        if (mcp.command) lines.push(`    command: ${yamlQuote(mcp.command)}`);
        if (mcp.args?.length) {
          lines.push(`    args: [${mcp.args.map((a) => yamlQuote(a)).join(", ")}]`);
        }
        if (mcp.env && Object.keys(mcp.env).length > 0) {
          lines.push("    env:");
          for (const [k, v] of Object.entries(mcp.env)) {
            lines.push(`      ${k}: ${yamlQuote(v)}`);
          }
        }
      } else {
        if (mcp.url) lines.push(`    url: ${yamlQuote(mcp.url)}`);
        if (mcp.headers && Object.keys(mcp.headers).length > 0) {
          lines.push("    headers:");
          for (const [k, v] of Object.entries(mcp.headers)) {
            lines.push(`      ${k}: ${yamlQuote(v)}`);
          }
        }
      }
    }
  }
  lines.push("");

  if (input.instructions) {
    lines.push("instructions: AGENTS.md");
    lines.push("");
  }

  const configYaml = lines.join("\n");

  // Build tar archive
  const files: TarEntry[] = [
    { name: "config.yaml", content: new TextEncoder().encode(configYaml) },
  ];
  if (input.instructions) {
    files.push({ name: "AGENTS.md", content: new TextEncoder().encode(input.instructions) });
  }

  const tarBytes = createTar(files);
  const gzipped = await gzip(tarBytes);
  return new File([gzipped.buffer as ArrayBuffer], "agent.tar.gz", {
    type: "application/gzip",
  });
}

// ── Helpers ──────────────────────────────────────────────────────

/** Quote a YAML string value if it contains special characters. */
function yamlQuote(s: string): string {
  if (/[:\n"'#{}[\],&*?|>!%@`]/.test(s) || s.trim() !== s) {
    return JSON.stringify(s);
  }
  return s;
}

interface TarEntry {
  name: string;
  content: Uint8Array;
}

/**
 * Create a minimal POSIX tar archive (uncompressed) from file entries.
 * Each entry gets a 512-byte header followed by content padded to 512.
 * Archive ends with two 512-byte zero blocks.
 */
function createTar(entries: TarEntry[]): Uint8Array {
  const blocks: Uint8Array[] = [];

  for (const entry of entries) {
    const header = new Uint8Array(512);
    const encoder = new TextEncoder();

    // File name (0..99)
    const nameBytes = encoder.encode(entry.name);
    header.set(nameBytes.slice(0, 100), 0);

    // File mode (100..107) — 0644
    writeOctal(header, 100, 8, 0o644);
    // Owner ID (108..115)
    writeOctal(header, 108, 8, 0);
    // Group ID (116..123)
    writeOctal(header, 116, 8, 0);
    // File size (124..135)
    writeOctal(header, 124, 12, entry.content.length);
    // Modification time (136..147) — current time
    writeOctal(header, 136, 12, Math.floor(Date.now() / 1000));
    // Type flag (156) — '0' for regular file
    header[156] = 0x30;
    // Magic (257..262) — "ustar\0"
    header.set(encoder.encode("ustar\0"), 257);
    // Version (263..264) — "00"
    header.set(encoder.encode("00"), 263);

    // Checksum (148..155) — fill with spaces first, compute, then write
    for (let i = 148; i < 156; i++) header[i] = 0x20;
    let checksum = 0;
    for (let i = 0; i < 512; i++) checksum += header[i];
    writeOctal(header, 148, 7, checksum);
    header[155] = 0x20; // trailing space per POSIX

    blocks.push(header);

    // Content blocks (padded to 512-byte boundary)
    const contentBlocks = Math.ceil(entry.content.length / 512);
    const padded = new Uint8Array(contentBlocks * 512);
    padded.set(entry.content);
    blocks.push(padded);
  }

  // End-of-archive marker: two 512-byte zero blocks
  blocks.push(new Uint8Array(1024));

  // Concatenate all blocks
  const totalSize = blocks.reduce((sum, b) => sum + b.length, 0);
  const result = new Uint8Array(totalSize);
  let offset = 0;
  for (const block of blocks) {
    result.set(block, offset);
    offset += block.length;
  }
  return result;
}

/** Write a number as null-terminated octal string into a tar header field. */
function writeOctal(header: Uint8Array, offset: number, length: number, value: number): void {
  const str = value.toString(8).padStart(length - 1, "0");
  const encoder = new TextEncoder();
  const bytes = encoder.encode(str);
  header.set(bytes.slice(0, length - 1), offset);
  header[offset + length - 1] = 0; // null terminator
}

/** Gzip compress bytes using the Compression Streams API. */
async function gzip(data: Uint8Array): Promise<Uint8Array> {
  const cs = new CompressionStream("gzip");
  const writer = cs.writable.getWriter();
  writer.write(data.buffer as ArrayBuffer);
  writer.close();

  const reader = cs.readable.getReader();
  const chunks: Uint8Array[] = [];
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
  }

  const totalSize = chunks.reduce((sum, c) => sum + c.length, 0);
  const result = new Uint8Array(totalSize);
  let offset = 0;
  for (const chunk of chunks) {
    result.set(chunk, offset);
    offset += chunk.length;
  }
  return result;
}
