import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join, relative } from "node:path";
import { describe, expect, it } from "vitest";

const SOURCE_ROOT = existsSync(join(process.cwd(), "src", "actions"))
  ? join(process.cwd(), "src")
  : join(process.cwd(), "web", "src");

const GLOBAL_KEY_LISTENER_ALLOWLIST: Readonly<Record<string, string>> = {
  "actions/KeybindingDispatcher.tsx": "the centralized application shortcut dispatcher",
  "components/OttoEyes.tsx": "decorative key-up activity used to animate the mascot",
  "hooks/useIdleNotifications.ts": "generic interaction and notification-permission detection",
};

function productionSources(directory = SOURCE_ROOT): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) {
      return relative(SOURCE_ROOT, path) === "components/ai-elements"
        ? []
        : productionSources(path);
    }
    if (!/\.tsx?$/.test(entry.name) || /\.(?:test|stories)\./.test(entry.name)) return [];
    return [path];
  });
}

describe("centralized shortcut architecture", () => {
  it("keeps global keyboard listeners in the dispatcher or documented non-shortcut adapters", () => {
    const listeners = productionSources().flatMap((path) => {
      const source = readFileSync(path, "utf8");
      return /\b(?:window|globalThis|document(?:\.body|\.documentElement)?)\??\.addEventListener\(\s*["']key(?:down|up|press)["']/.test(
        source,
      )
        ? [relative(SOURCE_ROOT, path)]
        : [];
    });
    expect(listeners.sort()).toEqual(Object.keys(GLOBAL_KEY_LISTENER_ALLOWLIST).sort());
  });

  it("does not reintroduce legacy hotkey hooks or direct editor key commands", () => {
    const violations = productionSources().flatMap((path) => {
      const source = readFileSync(path, "utf8");
      return [
        ["react-hotkeys-hook", /from\s+["']react-hotkeys-hook["']/],
        ["Monaco addCommand", /\.addCommand\s*\(/],
        ["Monaco addAction keybindings", /\.addAction\s*\(\s*\{[\s\S]{0,500}\bkeybindings\s*:/],
        ["editor key listener", /\beditor\.onKeyDown\s*\(/],
        ["xterm custom key handler", /attachCustomKeyEventHandler\s*\(|\.onKey\s*\(/],
      ].flatMap(([label, pattern]) =>
        (pattern as RegExp).test(source) ? [`${relative(SOURCE_ROOT, path)}: ${label}`] : [],
      );
    });
    expect(violations).toEqual([]);
  });

  it("keeps file-search Escape ownership in the centralized action", () => {
    for (const file of ["shell/MarkdownSearchBar.tsx", "shell/PreviewSearchBar.tsx"]) {
      expect(readFileSync(join(SOURCE_ROOT, file), "utf8")).not.toMatch(/\.key\s*===\s*["']Escape/);
    }
  });
});
