import { describe, expect, it } from "vitest";
import { mcpServerDisplayName } from "./mcpServerName";

describe("mcpServerDisplayName", () => {
  it.each([
    ["github", "GitHub"],
    ["GitHub", "GitHub"],
    ["GITHUB", "GitHub"],
    ["gitlab", "GitLab"],
    ["linear", "Linear"],
    ["slack", "Slack"],
    ["jira", "Jira"],
    ["confluence", "Confluence"],
    ["notion", "Notion"],
    ["figma", "Figma"],
    ["sentry", "Sentry"],
    ["pagerduty", "PagerDuty"],
    ["glean", "Glean"],
    ["Databricks", "Databricks"],
  ])("shows the known server %s as %s", (name, displayName) => {
    expect(mcpServerDisplayName(name)).toBe(displayName);
  });

  it.each([
    ["databricks-v2", "databricks-v2"],
    ["linear-server", "linear-server"],
    ["github-mcp", "github-mcp"],
    ["MyCRM", "mycrm"],
    ["docs", "docs"],
  ])("shows the unlisted server %s lowercased as %s", (name, displayName) => {
    expect(mcpServerDisplayName(name)).toBe(displayName);
  });

  it("does not treat object prototype keys as known names", () => {
    expect(mcpServerDisplayName("constructor")).toBe("constructor");
  });
});
