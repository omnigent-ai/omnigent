import { useState } from "react";
import type { Meta, StoryObj } from "@storybook/react-vite";
import { useCommandPaletteHotkey } from "@/hooks/useCommandPaletteHotkey";
import { useLocation } from "@/lib/routing";
import { StoryQueryRouter } from "@/storybook/StoryProviders";
import { CommandPalette } from "./CommandPalette";

const sessions = [
  { id: "parser", title: "Fix the parser" },
  { id: "deploy", title: "Deploy API" },
  { id: "docs", title: "Update documentation" },
];

function SessionSearch() {
  const activeId = useLocation().pathname.split("/").at(-1);
  const [open, setOpen] = useState(false);
  const [sessionsOnly, setSessionsOnly] = useState(true);
  useCommandPaletteHotkey(
    () => {
      setSessionsOnly(false);
      setOpen(true);
    },
    true,
    undefined,
    () => {
      setSessionsOnly(true);
      setOpen(true);
    },
  );
  return (
    <main className="w-[600px] space-y-4 rounded-xl border bg-background p-6 text-foreground">
      <h1 className="text-xl font-semibold">
        {sessions.find((session) => session.id === activeId)?.title}
      </h1>
      <p className="text-sm text-muted-foreground">
        Press Command+Option+S (Control+Alt+S on Windows/Linux) to search sessions by name. Try
        “fxprs” to find “Fix the parser”.
      </p>
      <button
        type="button"
        className="rounded border px-3 py-2 text-sm"
        onClick={() => {
          setSessionsOnly(true);
          setOpen(true);
        }}
      >
        Find a session by name
      </button>
      <textarea
        aria-label="Composer"
        className="w-full rounded border p-3 text-sm"
        placeholder="Search also works while composing…"
      />
      <CommandPalette
        key={sessionsOnly ? "sessions" : "commands"}
        open={open}
        sessionsOnly={sessionsOnly}
        onOpenChange={setOpen}
        onToggleLeftSidebar={() => undefined}
        onToggleRightSidebar={() => undefined}
      />
    </main>
  );
}

const meta = {
  title: "Components/Shell/SessionSearch",
  component: SessionSearch,
  decorators: [
    (Story) => (
      <StoryQueryRouter
        route="/c/deploy"
        seed={(client) => {
          const data = {
            pages: [
              {
                data: sessions.map((session) => ({
                  ...session,
                  object: "conversation",
                  archived: false,
                  agent_name: "Assistant",
                  labels: {},
                  created_at: 1,
                  updated_at: 1,
                })),
                has_more: false,
              },
            ],
            pageParams: [undefined],
          };
          client.setQueryData(["conversations", "", false], data);
          client.setQueryData(["conversations", "", true], data);
        }}
      >
        <Story />
      </StoryQueryRouter>
    ),
  ],
} satisfies Meta<typeof SessionSearch>;

export default meta;
type Story = StoryObj<typeof meta>;
export const Default: Story = {};
