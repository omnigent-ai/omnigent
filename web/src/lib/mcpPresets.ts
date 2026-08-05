import type { MCPServerInput } from "./agentBundle";

export type MCPIntegrationIcon = "book" | "browser" | "cloud" | "data" | "dev-tools" | "search";
export type MCPIntegrationAuth = "ambient" | "none";

export interface MCPIntegrationField {
  id: string;
  label: string;
  placeholder?: string;
  required?: boolean;
}

export interface MCPServerPreset {
  id: string;
  provider: string;
  category:
    | "Browser automation"
    | "Cloud infrastructure"
    | "Data and analytics"
    | "Developer tools"
    | "Documentation"
    | "Web and research";
  label: string;
  description: string;
  prerequisites?: string;
  auth: MCPIntegrationAuth;
  icon: MCPIntegrationIcon;
  fields?: MCPIntegrationField[];
  create: (values: Record<string, string>) => MCPServerInput;
}

const MCP_INTEGRATION_CATEGORY_ORDER: readonly MCPServerPreset["category"][] = [
  "Documentation",
  "Web and research",
  "Browser automation",
  "Cloud infrastructure",
  "Developer tools",
  "Data and analytics",
];

export const MCP_SERVER_PRESETS: readonly MCPServerPreset[] = [
  {
    id: "microsoft-learn",
    provider: "Microsoft",
    category: "Documentation",
    label: "Microsoft Learn",
    description: "Search current Microsoft documentation and code samples.",
    prerequisites: "Public and free.",
    auth: "none",
    icon: "book",
    create: () => ({
      name: "microsoft-learn",
      transport: "http",
      url: "https://learn.microsoft.com/api/mcp",
    }),
  },
  {
    id: "aws-knowledge",
    provider: "AWS",
    category: "Documentation",
    label: "AWS Knowledge",
    description: "Search AWS documentation, announcements, and architecture guidance.",
    prerequisites: "Public and free. Subject to rate limits.",
    auth: "none",
    icon: "book",
    create: () => ({
      name: "aws-knowledge",
      transport: "http",
      url: "https://knowledge-mcp.global.api.aws",
    }),
  },
  {
    id: "deepwiki",
    provider: "Devin",
    category: "Documentation",
    label: "DeepWiki",
    description:
      "Read repository documentation and ask grounded questions about public GitHub repos.",
    prerequisites: "Public and free for public repositories.",
    auth: "none",
    icon: "book",
    create: () => ({
      name: "deepwiki",
      transport: "http",
      url: "https://mcp.deepwiki.com/mcp",
    }),
  },
  {
    id: "cloudflare-docs",
    provider: "Cloudflare",
    category: "Documentation",
    label: "Cloudflare Docs",
    description: "Search current Cloudflare developer documentation.",
    prerequisites: "Public documentation endpoint.",
    auth: "none",
    icon: "book",
    create: () => ({
      name: "cloudflare-docs",
      transport: "http",
      url: "https://docs.mcp.cloudflare.com/mcp",
    }),
  },
  {
    id: "cloudflare-agents-docs",
    provider: "Cloudflare",
    category: "Documentation",
    label: "Cloudflare Agents SDK Docs",
    description: "Search token-efficient documentation for Cloudflare's Agents SDK.",
    prerequisites: "Public documentation endpoint.",
    auth: "none",
    icon: "book",
    create: () => ({
      name: "cloudflare-agents-docs",
      transport: "http",
      url: "https://agents.cloudflare.com/mcp",
    }),
  },
  {
    id: "cloudflare-blog",
    provider: "Cloudflare",
    category: "Documentation",
    label: "Cloudflare Blog",
    description: "Search and read technical posts from the Cloudflare Blog.",
    prerequisites: "Public content endpoint.",
    auth: "none",
    icon: "book",
    create: () => ({
      name: "cloudflare-blog",
      transport: "http",
      url: "https://blog.mcp.cloudflare.com/mcp",
    }),
  },
  {
    id: "context7",
    provider: "Upstash",
    category: "Documentation",
    label: "Context7",
    description: "Retrieve current library and framework documentation and examples.",
    prerequisites: "Anonymous usage is rate-limited. Add a custom server later for API-key access.",
    auth: "none",
    icon: "book",
    create: () => ({
      name: "context7",
      transport: "http",
      url: "https://mcp.context7.com/mcp",
    }),
  },
  {
    id: "exa-search",
    provider: "Exa",
    category: "Web and research",
    label: "Exa Search",
    description: "Search the web and fetch pages as clean, agent-ready content.",
    prerequisites: "Anonymous usage has a free, rate-limited quota.",
    auth: "none",
    icon: "search",
    create: () => ({
      name: "exa-search",
      transport: "http",
      url: "https://mcp.exa.ai/mcp",
    }),
  },
  {
    id: "exa-fetch",
    provider: "Exa",
    category: "Web and research",
    label: "Exa Fetch",
    description: "Fetch known webpages as clean Markdown without exposing search tools.",
    prerequisites: "Anonymous usage has a free, rate-limited quota.",
    auth: "none",
    icon: "search",
    create: () => ({
      name: "exa-fetch",
      transport: "http",
      url: "https://mcp.exa.ai/mcp?tools=web_fetch_exa",
    }),
  },
  {
    id: "playwright",
    provider: "Microsoft",
    category: "Browser automation",
    label: "Playwright",
    description: "Automate browser workflows using structured accessibility snapshots.",
    prerequisites: "Requires Node.js 18+, npx, and a Chromium-compatible browser on the host.",
    auth: "none",
    icon: "browser",
    create: () => ({
      name: "playwright",
      transport: "stdio",
      command: "npx",
      args: ["-y", "@playwright/mcp@latest", "--isolated"],
    }),
  },
  {
    id: "azure",
    provider: "Microsoft",
    category: "Cloud infrastructure",
    label: "Azure",
    description: "Inspect Azure resources through the consolidated Azure tool.",
    prerequisites: "Requires Node.js, npx, Azure CLI, and az login on the selected host.",
    auth: "ambient",
    icon: "cloud",
    create: () => ({
      name: "azure",
      transport: "stdio",
      command: "npx",
      args: ["-y", "@azure/mcp@latest", "server", "start", "--mode", "single", "--read-only"],
    }),
  },
  {
    id: "aws-documentation",
    provider: "AWS",
    category: "Cloud infrastructure",
    label: "AWS Documentation",
    description: "Search AWS documentation through the local AWS Labs server.",
    prerequisites: "Requires uv/uvx on the selected host. No AWS credentials required.",
    auth: "none",
    icon: "cloud",
    create: () => ({
      name: "aws-documentation",
      transport: "stdio",
      command: "uvx",
      args: ["awslabs.aws-documentation-mcp-server@latest"],
    }),
  },
  {
    id: "terraform-registry",
    provider: "HashiCorp",
    category: "Cloud infrastructure",
    label: "Terraform Registry",
    description: "Search public Terraform providers, modules, and policies.",
    prerequisites: "Requires a running Docker installation on the selected host.",
    auth: "none",
    icon: "cloud",
    create: () => ({
      name: "terraform-registry",
      transport: "stdio",
      command: "docker",
      args: ["run", "-i", "--rm", "hashicorp/terraform-mcp-server:1.1.0"],
    }),
  },
  {
    id: "azure-devops",
    provider: "Microsoft",
    category: "Developer tools",
    label: "Azure DevOps",
    description: "Work with repositories, pull requests, work items, pipelines, wikis, and tests.",
    prerequisites: "Requires Node.js 20+, npx, Azure CLI, and az login on the selected host.",
    auth: "ambient",
    icon: "dev-tools",
    fields: [
      {
        id: "organization",
        label: "Organization",
        placeholder: "contoso",
        required: true,
      },
    ],
    create: (values) => ({
      name: "azure-devops",
      transport: "stdio",
      command: "npx",
      args: [
        "-y",
        "@azure-devops/mcp",
        values.organization?.trim() ?? "",
        "--authentication",
        "azcli",
      ],
    }),
  },
  {
    id: "aws-open-data",
    provider: "AWS",
    category: "Data and analytics",
    label: "AWS Open Data",
    description: "Discover and preview public datasets in the Registry of Open Data on AWS.",
    prerequisites: "Requires uv/uvx on the selected host. No AWS account required.",
    auth: "none",
    icon: "data",
    create: () => ({
      name: "aws-open-data",
      transport: "stdio",
      command: "uvx",
      args: ["awslabs.roda-mcp-server@latest"],
    }),
  },
];

export function getMCPServerPreset(id: string): MCPServerPreset | undefined {
  return MCP_SERVER_PRESETS.find((preset) => preset.id === id);
}

export function groupMCPServerPresets(
  presets: readonly MCPServerPreset[] = MCP_SERVER_PRESETS,
): [MCPServerPreset["category"], MCPServerPreset[]][] {
  const groups = new Map<MCPServerPreset["category"], MCPServerPreset[]>();
  for (const preset of presets) {
    const group = groups.get(preset.category) ?? [];
    group.push(preset);
    groups.set(preset.category, group);
  }
  return MCP_INTEGRATION_CATEGORY_ORDER.flatMap((category) => {
    const group = groups.get(category);
    return group ? [[category, group]] : [];
  });
}
