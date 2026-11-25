import { readFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";

// `databricks auth token --host` fails when several profiles share a host, even
// though `?o=<org-id>` names one workspace. Like omnigent's Python resolver
// (`_prefer_profiles_for_workspace`), pick the profile pinned to that workspace.
export function profileForWorkspace(
  configText: string,
  host: string,
  workspaceId: string,
): string | undefined {
  const sections = new Map<string, Record<string, string>>();
  let current: Record<string, string> | undefined;
  for (const line of configText.split(/\r?\n/)) {
    const trimmed = line.trim();
    const section = /^\[(.+)\]$/.exec(trimmed);
    if (section) {
      current = {};
      sections.set(section[1].trim(), current);
      continue;
    }
    const entry = /^([^=#;]+?)\s*=\s*(.*)$/.exec(trimmed);
    if (entry && current) current[entry[1].toLowerCase()] = entry[2].trim();
  }
  // A section without its own host inherits [DEFAULT]'s, as in ~/.databrickscfg.
  const defaultHost = sections.get("DEFAULT")?.host;
  const pinned = [...sections].filter(
    ([name, options]) =>
      name !== "DEFAULT" &&
      hostMatchKey(options.host ?? defaultHost) === hostMatchKey(host) &&
      options.workspace_id === workspaceId,
  );
  // Several pinned profiles stay ambiguous; the CLI then reports them.
  return pinned.length === 1 ? pinned[0][0] : undefined;
}

// Scheme, case and a trailing `/` are ignored, as in Python's `_host_match_key`.
function hostMatchKey(host: string | undefined): string | undefined {
  if (!host) return undefined;
  const bare = host.trim().replace(/\/+$/, "");
  return bare.slice(bare.includes("://") ? bare.indexOf("://") + 3 : 0).toLowerCase();
}

// Reads the config file the CLI reads; a missing file means no pinned profile.
export function databricksProfileForWorkspace(
  host: string,
  workspaceId: string,
): string | undefined {
  const configured = process.env.DATABRICKS_CONFIG_FILE;
  const file = configured
    ? configured.replace(/^~(?=$|\/)/, os.homedir())
    : path.join(os.homedir(), ".databrickscfg");
  try {
    return profileForWorkspace(readFileSync(file, "utf8"), host, workspaceId);
  } catch {
    return undefined;
  }
}
