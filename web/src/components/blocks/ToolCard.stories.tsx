import type { Meta, StoryObj } from "@storybook/react-vite";
import { userEvent, within } from "storybook/test";
import { writeTerminalThemeMode } from "@/lib/terminalThemePreferences";
import { StoryQueryRouter } from "@/storybook/StoryProviders";
import { ToolCard } from "./ToolCard";

const longOutput = Array.from(
  { length: 85 },
  (_, index) => `Line ${String(index + 1).padStart(2, "0")}: deterministic tool output`,
).join("\n");

const longCommand =
  '/bin/zsh -lc \'PATH="$HOME/.nvm/versions/node/v22.22.3/bin:$PATH" pnpm install ' +
  "--lockfile-only --ignore-scripts --filter @omnigent/web --filter @omnigent/desktop " +
  "--reporter=append-only > /tmp/install.log 2>&1'";

const longLineOutput = [
  "Scope: all 14 workspace projects",
  `WARN  deprecated subdependencies found: ${Array.from(
    { length: 12 },
    (_, index) => `@scope/legacy-package-${index}@1.0.${index}`,
  ).join(", ")}`,
  "Lockfile is up to date, resolution step is skipped",
  "Done in 13.6s using pnpm v10.17.1",
].join("\n");

const pnpmOutput = Array.from(
  { length: 85 },
  (_, index) => `Progress: resolved ${(index + 1) * 20}, reused ${(index + 1) * 20}, downloaded 0`,
).join("\n");

const meta = {
  title: "Components/Blocks/ToolCard",
  component: ToolCard,
  tags: ["visual-snapshot"],
  // Cards span the chat column in the app; the centered canvas would shrink them to their content.
  decorators: [
    (Story) => (
      <div className="w-[42rem] max-w-full">
        <Story />
      </div>
    ),
  ],
} satisfies Meta<typeof ToolCard>;

export default meta;
type Story = StoryObj<typeof meta>;

async function expand(canvasElement: HTMLElement): Promise<void> {
  const trigger = canvasElement.querySelector<HTMLElement>('[data-slot="collapsible-trigger"]');
  if (!trigger) throw new Error("Tool card trigger not found");
  await userEvent.click(trigger);
  // Snapshot the resting state, not the keyboard focus ring the click leaves.
  trigger.blur();
}

export const CompletedFileRead: Story = {
  args: {
    name: "sys_os_read",
    arguments: { path: "src/components/blocks/ToolCard.tsx" },
    output: JSON.stringify({
      path: "src/components/blocks/ToolCard.tsx",
      content: 'import { useMemo } from "react";\n\nexport function ToolCard() {}\n',
    }),
    state: "output-available",
    startedAt: null,
    duration: 3.25,
  },
};

export const LongOutputPreview: Story = {
  args: {
    name: "component_audit",
    arguments: { mode: "visual", requestedLines: 85 },
    output: longOutput,
    state: "output-available",
    startedAt: null,
    duration: 1.2,
  },
  play: async ({ canvasElement }) => expand(canvasElement),
};

// A tool with no formatter: humanized title, key argument values, and the
// expanded input as labeled fields instead of a JSON dump.
export const GenericTodoListExpanded: Story = {
  args: {
    name: "manage_todo_list",
    arguments: {
      operation: "write",
      todoList: [
        {
          id: 1,
          title: "Find review workflow and GitHub identity",
          description: "Loaded review-queue skill; authenticated as the reviewer.",
          status: "completed",
        },
        {
          id: 2,
          title: "Fetch open PRs awaiting your review and prioritize them",
          description: "Checking review history, CI and mergeability.",
          status: "in-progress",
        },
      ],
    },
    output: "Task plan updated (2 tasks).",
    state: "output-available",
    startedAt: null,
    duration: 0.061,
  },
  play: async ({ canvasElement }) => expand(canvasElement),
};

// MCP results arrive as content blocks wrapping the server's JSON.
// A field selector stays out of the title, and the markdown description renders as prose.
export const McpJsonResultExpanded: Story = {
  args: {
    name: "mcp__linear__get_issue",
    arguments: { id: "OMNI-12243", fields: ["id", "title", "description", "state", "labels"] },
    output: JSON.stringify([
      {
        type: "text",
        text: JSON.stringify({
          id: "OMNI-12243",
          title: "Claude mod replaces tmux paste for native turns",
          description:
            "Follow-up to [#11607](<https://github.com/omnigent-ai/omnigent/pull/11607>).\n\n" +
            "1. Send turns through the **mod** channel.\n2. Keep tmux paste as the fallback.",
          state: "In Review",
          priority: "P1",
          labels: ["claude-native", "harness"],
          url: "https://linear.app/omnigent/issue/OMNI-12243",
        }),
      },
    ]),
    state: "output-available",
    startedAt: null,
    duration: 0.412,
  },
  // Markdown links check workspace files through React Query, as in the app.
  decorators: [
    (Story) => (
      <StoryQueryRouter>
        <Story />
      </StoryQueryRouter>
    ),
  ],
  play: async ({ canvasElement }) => expand(canvasElement),
};

// A failure the tool reported reads in red on the collapsed row.
export const McpFailureCollapsed: Story = {
  args: {
    name: "mcp__github__create_pull_request",
    arguments: {
      owner: "omnigent-ai",
      repo: "omnigent",
      head: "shell-card-pinned-selection",
      base: "main",
      draft: true,
    },
    output:
      "Error: 422 Validation Failed: A pull request already exists for " +
      "omnigent-ai:shell-card-pinned-selection.",
    state: "output-available",
    startedAt: null,
    duration: 1.2,
  },
};

// A Claude edit: its line counts on the result line, its diff when expanded.
export const EditExpanded: Story = {
  args: {
    name: "Edit",
    arguments: {
      file_path: "/repo/src/greet.py",
      old_string:
        "def greet(name):\n    print('Hello ' + name)\n    print('Welcome back')\n    return name\n",
      new_string:
        "def greet(name):\n    message = f'Hello {name}! Welcome back.'\n    print(message)\n    return name\n",
    },
    output: "The file /repo/src/greet.py has been updated successfully.",
    state: "output-available",
    startedAt: null,
    duration: 0.009,
  },
  play: async ({ canvasElement }) => expand(canvasElement),
};

// A Codex patch over three files: one panel per file, named with its change kind.
export const PatchExpanded: Story = {
  args: {
    name: "apply_patch",
    arguments: {
      changes: [
        {
          path: "/repo/src/greet.py",
          kind: { type: "update" },
          diff: "@@ -1,3 +1,3 @@\n def greet(name):\n-    print('Hello ' + name)\n+    print(f'Hello {name}')\n     return name\n",
        },
        {
          path: "/repo/src/farewell.py",
          kind: { type: "add" },
          diff: "def farewell(name):\n    print(f'Bye {name}')\n",
        },
        { path: "/repo/src/legacy.py", kind: { type: "delete" }, diff: "LEGACY = True\n" },
      ],
    },
    output:
      "File change applied.\nupdate /repo/src/greet.py\nadd /repo/src/farewell.py\ndelete /repo/src/legacy.py",
    state: "output-available",
    startedAt: null,
    duration: 0.04,
  },
  play: async ({ canvasElement }) => expand(canvasElement),
};

// A missed edit: the error, then the requested change labeled as not applied.
export const FailedEditExpanded: Story = {
  args: {
    name: "Edit",
    arguments: { file_path: "/repo/README.md", old_string: "Hi", new_string: "Hello" },
    output: "<tool_use_error>String to replace not found in file.</tool_use_error>",
    state: "output-available",
    startedAt: null,
    duration: 0.006,
  },
  play: async ({ canvasElement }) => expand(canvasElement),
};

export const ScheduledTaskExpanded: Story = {
  args: {
    name: "sys_scheduled_task_create",
    arguments: {
      name: "weekday-review-queue",
      schedule: "0 9 * * 1-5",
      prompt: "Check PRs awaiting my review and post a summary.",
      enabled: true,
    },
    output: JSON.stringify({ task_id: "st_8f21c0", next_run_at: "2026-10-27T16:00:00Z" }),
    state: "output-available",
    startedAt: null,
    duration: 0.088,
  },
  play: async ({ canvasElement }) => expand(canvasElement),
};

export const RunningWithPinnedDuration: Story = {
  args: {
    name: "web_search",
    arguments: { query: "deterministic component states" },
    output: null,
    state: "input-available",
    startedAt: null,
    duration: 2.5,
  },
  play: async ({ canvasElement }) => expand(canvasElement),
};

export const FailedExpanded: Story = {
  args: {
    name: "sys_os_shell",
    arguments: { command: "exit 1" },
    output: null,
    state: "output-error",
    startedAt: null,
    duration: 0.042,
  },
  play: async ({ canvasElement }) => expand(canvasElement),
};

export const ShellCompleted: Story = {
  args: {
    name: "shell",
    arguments: { command: "/bin/zsh -lc 'git status --short'", cwd: "/home/dev/omnigent" },
    output: " M web/package.json\n M pnpm-workspace.yaml\n?? web/src/lib/newFile.ts",
    state: "output-available",
    startedAt: null,
    duration: 0.08,
  },
  play: async ({ canvasElement }) => expand(canvasElement),
};

export const ShellFailedWithExitCode: Story = {
  args: {
    name: "shell",
    arguments: { command: "/bin/bash -lc 'pnpm install --frozen-lockfile'", cwd: "/repo" },
    output:
      "ERR_PNPM_OUTDATED_LOCKFILE  Cannot install with frozen-lockfile because pnpm-lock.yaml " +
      "is not up to date with web/package.json\n[exit code: 1]",
    state: "output-available",
    startedAt: null,
    duration: 3.1,
  },
  play: async ({ canvasElement }) => expand(canvasElement),
};

export const ShellLongLinesWrapped: Story = {
  args: {
    name: "shell",
    arguments: {
      command: longCommand,
      cwd: "/home/dev/omnigent",
    },
    output: longLineOutput,
    state: "output-available",
    startedAt: null,
    duration: 13.6,
  },
  play: async ({ canvasElement }) => expand(canvasElement),
};

export const ShellLongLinesScrolling: Story = {
  args: ShellLongLinesWrapped.args,
  play: async ({ canvasElement }) => {
    await expand(canvasElement);
    const wrapToggle = within(canvasElement).getByRole("button", { name: "Disable word wrap" });
    await userEvent.click(wrapToggle);
    wrapToggle.blur();
  },
};

export const ShellManyLinesOutput: Story = {
  args: {
    name: "Bash",
    arguments: { command: "pnpm install --reporter=append-only" },
    output: pnpmOutput,
    state: "output-available",
    startedAt: null,
    duration: 42,
  },
  play: async ({ canvasElement }) => expand(canvasElement),
};

export const ShellRunning: Story = {
  args: {
    name: "Bash",
    arguments: { command: 'PATH="$HOME/.nvm/versions/node/v22.22.3/bin:$PATH" pnpm test' },
    output: null,
    state: "input-available",
    startedAt: null,
    duration: 12,
  },
  play: async ({ canvasElement }) => expand(canvasElement),
};

// sys_os_shell reports its working directory in the result, not the arguments.
export const ShellSysOsShellResultCwd: Story = {
  args: {
    name: "sys_os_shell",
    arguments: { command: "git log --oneline -3" },
    output: JSON.stringify({
      stdout:
        "3f2a9c1 Add the settings page\n" +
        "8b1e0d4 Fix the login redirect\n" +
        "c47a2e9 Update dependencies\n",
      stderr: "",
      exit_code: 0,
      timed_out: false,
      cwd: "/home/dev/omnigent",
    }),
    state: "output-available",
    startedAt: null,
    duration: 0.05,
  },
  play: async ({ canvasElement }) => expand(canvasElement),
};

// Claude's Bash carries no cwd, so the actions share the command's line.
export const ShellLongCommandWithoutCwd: Story = {
  args: {
    name: "shell",
    arguments: { command: longCommand },
    output: "Lockfile is up to date, resolution step is skipped",
    state: "output-available",
    startedAt: null,
    duration: 13.6,
  },
  play: async ({ canvasElement }) => expand(canvasElement),
};

export const ShellAnsiColors: Story = {
  args: {
    name: "Bash",
    arguments: { command: "git diff --stat --color=always origin/main" },
    output: [
      " web/package.json    | 2 \u001b[32m+\u001b[m\u001b[31m-\u001b[m",
      " pnpm-workspace.yaml | 3 \u001b[32m++\u001b[m\u001b[31m-\u001b[m",
      " 2 files changed, 3 insertions(+), 2 deletions(-)",
      "\u001b[1;33mwarning:\u001b[0m CRLF will be replaced by LF in web/package.json",
    ].join("\n"),
    state: "output-available",
    startedAt: null,
    duration: 0.04,
  },
  play: async ({ canvasElement }) => expand(canvasElement),
};

const multiLineScript = [
  "git diff --check",
  "git status --short",
  "git add web/src/lib/format.ts web/src/lib/format.test.ts " +
    "web/src/components/blocks/ToolCard.tsx web/src/components/blocks/ToolCard.test.ts",
  "cat > /tmp/commit-message.txt <<'EOF'",
  "fix(web): prompt each shell command",
  "EOF",
  'PATH="$HOME/.nvm/versions/node/v22.22.3/bin:$PATH" git commit -s \\',
  "  -F /tmp/commit-message.txt > /tmp/commit.log 2>&1",
].join("\n");

// The row shows the first line and a line count; the panel keeps one prompt,
// and wrapped text indents under it.
export const ShellMultiLineScript: Story = {
  args: {
    name: "Bash",
    arguments: { command: multiLineScript },
    output: " M web/src/components/blocks/ToolCard.tsx\n?? web/src/lib/format.ts",
    state: "output-available",
    startedAt: null,
    duration: 0.31,
  },
  play: async ({ canvasElement }) => expand(canvasElement),
};

// Settings > Terminal theme pinned to dark while the app is light.
export const ShellDarkTerminalInLightApp: Story = {
  args: {
    name: "shell",
    arguments: {
      command: "/bin/zsh -lc 'git diff --stat --color=always origin/main'",
      cwd: "/home/dev/omnigent",
    },
    output: ShellAnsiColors.args.output,
    state: "output-available",
    startedAt: null,
    duration: 0.04,
  },
  beforeEach: () => {
    writeTerminalThemeMode("dark");
    return () => writeTerminalThemeMode("auto");
  },
  play: async ({ canvasElement }) => expand(canvasElement),
};

// A phone-width card: the actions get their own row instead of crowding the
// first command line or the folder.
const narrowCard: Story["decorators"] = [
  (Story) => (
    <div className="w-80">
      <Story />
    </div>
  ),
];

export const ShellNarrowWithoutCwd: Story = {
  args: {
    name: "shell",
    arguments: { command: longCommand },
    output: "Lockfile is up to date, resolution step is skipped",
    state: "output-available",
    startedAt: null,
    duration: 13.6,
  },
  decorators: narrowCard,
  play: async ({ canvasElement }) => expand(canvasElement),
};

export const ShellNarrowWithCwd: Story = {
  args: {
    name: "shell",
    arguments: {
      command: "/bin/zsh -lc 'git status --short'",
      cwd: "/home/dev/omnigent/web/src/components",
    },
    output: " M blocks/ToolCard.tsx\n?? blocks/NewCard.tsx",
    state: "output-available",
    startedAt: null,
    duration: 0.08,
  },
  decorators: narrowCard,
  play: async ({ canvasElement }) => expand(canvasElement),
};
