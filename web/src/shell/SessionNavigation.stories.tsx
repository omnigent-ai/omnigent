import { useState } from "react";
import type { Meta, StoryObj } from "@storybook/react-vite";
import { useCommandPaletteHotkey } from "@/hooks/useCommandPaletteHotkey";
import { usePinnedSessionHotkeys } from "@/hooks/usePinnedSessionHotkeys";
import { useRecentSessionHotkeys } from "@/hooks/useRecentSessionHotkeys";
import { useSessionSwitchHotkey } from "@/hooks/useSessionSwitchHotkey";
import { Link, useLocation } from "@/lib/routing";
import { StoryQueryRouter } from "@/storybook/StoryProviders";
import { CommandPalette } from "./CommandPalette";
import { SessionHotkeyHint, SessionHotkeyHints } from "./SessionHotkeyHints";

const sessions = [
  { id: "parser", title: "Fix the parser" },
  { id: "deploy", title: "Deploy API" },
  { id: "docs", title: "Update documentation" },
];
const ids = sessions.map((session) => session.id);

function SessionNavigation() {
  const activeId = useLocation().pathname.split("/").at(-1);
  const [open, setOpen] = useState(false);
  const [sessionsOnly, setSessionsOnly] = useState(true);
  useSessionSwitchHotkey(ids, activeId);
  usePinnedSessionHotkeys(ids, activeId);
  useRecentSessionHotkeys(ids, activeId);
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
    <div className="flex w-[760px] rounded-xl border bg-background text-foreground">
      <aside className="w-64 shrink-0 border-r p-4">
        <h2 className="mb-3 text-sm text-muted-foreground">Pinned sessions</h2>
        <SessionHotkeyHints ids={ids}>
          {sessions.map((session) => (
            <Link
              key={session.id}
              to={`/c/${session.id}`}
              className={`flex items-center rounded px-2 py-2 text-sm ${session.id === activeId ? "bg-muted" : ""}`}
            >
              <span className="min-w-0 truncate">{session.title}</span>
              <SessionHotkeyHint id={session.id} />
            </Link>
          ))}
        </SessionHotkeyHints>
      </aside>
      <main className="flex-1 space-y-4 p-6">
        <h1 className="text-xl font-semibold">
          {sessions.find((session) => session.id === activeId)?.title}
        </h1>
        <p className="text-sm text-muted-foreground">
          Hold Command (Control on Windows/Linux) to reveal session numbers. Use Command+[ or ] to
          switch adjacent sessions.
        </p>
        <p className="text-sm text-muted-foreground">
          Visit several sessions, then use Ctrl+backtick to cycle recent sessions in a browser.
          Release Control to commit the order.
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
          placeholder="Shortcuts also work while composing…"
        />
      </main>
      <CommandPalette
        key={sessionsOnly ? "sessions" : "commands"}
        open={open}
        sessionsOnly={sessionsOnly}
        onOpenChange={setOpen}
        onToggleLeftSidebar={() => undefined}
        onToggleRightSidebar={() => undefined}
      />
    </div>
  );
}

const meta = {
  title: "Components/Shell/SessionNavigation",
  component: SessionNavigation,
  decorators: [
    (Story) => (
      <StoryQueryRouter
        route="/c/parser"
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
} satisfies Meta<typeof SessionNavigation>;

export default meta;
type Story = StoryObj<typeof meta>;
export const Default: Story = {};
