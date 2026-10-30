// Brand spellings for well-known MCP servers, keyed by lowercase config name.
const KNOWN_SERVER_NAMES = new Map(
  [
    "GitHub",
    "GitLab",
    "Linear",
    "Slack",
    "Jira",
    "Confluence",
    "Notion",
    "Figma",
    "Sentry",
    "PagerDuty",
    "Glean",
    "Databricks",
  ].map((displayName) => [displayName.toLowerCase(), displayName]),
);

/** Brand spelling for a known MCP server name, else the name lowercased.
 * Only whole names match, so `linear-server` stays as written. */
export function mcpServerDisplayName(name: string): string {
  const lowercase = name.toLowerCase();
  return KNOWN_SERVER_NAMES.get(lowercase) ?? lowercase;
}
