import type { Meta, StoryObj } from "@storybook/react-vite";

import type { Conversation } from "@/hooks/useConversations";
import type { ConversationItem } from "@/lib/conversationItems";
import { StoryQueryRouter } from "@/storybook/StoryProviders";
import { ArchiveLibraryRail } from "./ArchiveLibraryRail";

const conversations: Conversation[] = [
  {
    id: "archive-example-1",
    object: "conversation",
    title: "Mobile archive layout review",
    created_at: 1_780_000_000,
    updated_at: 1_780_000_200,
    archived_at: 1_780_000_300,
    labels: { omni_project: "Example project" },
    permission_level: 3,
    archived: true,
    host_id: "host-example",
    agent_name: "Example agent",
  },
  {
    id: "archive-example-2",
    object: "conversation",
    title: "Searchable transcript notes",
    created_at: 1_779_900_000,
    updated_at: 1_779_900_200,
    archived_at: 1_779_900_300,
    labels: { omni_project: "Research" },
    permission_level: 3,
    archived: true,
    host_id: "host-lab",
    agent_name: "Research agent",
  },
];

const items: ConversationItem[] = [
  {
    id: "archive-example-user",
    response_id: "archive-example-response",
    type: "message",
    role: "user",
    status: "completed",
    content: [{ type: "input_text", text: "Check the Archive Library on mobile and desktop." }],
  },
  {
    id: "archive-example-assistant",
    response_id: "archive-example-response",
    type: "message",
    role: "assistant",
    status: "completed",
    model: "Example agent",
    content: [
      {
        type: "output_text",
        text: "The list, filters, transcript search, citations, and turn navigation fit the available width.",
      },
    ],
  },
];

const initialFilters = {
  searchQuery: "",
  searchScope: "title" as const,
  project: undefined,
  hostId: undefined,
  agentName: undefined,
  dateField: "archived_at" as const,
  dateRange: "",
  sortField: "archived_at" as const,
  agePreset: "any" as const,
  order: "desc" as const,
};

const meta = {
  title: "Components/Archive/LibraryRail",
  component: ArchiveLibraryRail,
  tags: ["visual-snapshot"],
  decorators: [
    (Story, context) => (
      <StoryQueryRouter
        route="/settings/archived"
        seed={(client) => {
          client.setQueryData(["archived-conversations", initialFilters, null], {
            data: conversations,
            first_id: conversations[0].id,
            last_id: conversations[1].id,
            has_more: false,
          });
          client.setQueryData(["archived-session-facets", initialFilters], {
            projects: ["Example project", "Research"],
            hostIds: ["host-example", "host-lab"],
            agentNames: ["Example agent", "Research agent"],
          });
          client.setQueryData(["projects"], []);
          client.setQueryData(
            ["hosts", { includeSandbox: true }],
            [
              { host_id: "host-example", name: "Example computer" },
              { host_id: "host-lab", name: "Research computer" },
            ],
          );
          client.setQueryData(["archive-transcript", conversations[0].id], {
            pages: [{ items, hasMore: false }],
            pageParams: [undefined],
          });
        }}
      >
        <div className="fixed inset-0 flex items-center justify-center overflow-hidden bg-background p-4">
          <div
            className="flex h-[720px] max-h-full overflow-hidden rounded-lg border bg-background"
            style={{ width: context.parameters.readerWidth ?? 390 }}
          >
            <Story />
          </div>
        </div>
      </StoryQueryRouter>
    ),
  ],
} satisfies Meta<typeof ArchiveLibraryRail>;

export default meta;
type Story = StoryObj<typeof meta>;

export const MobileList: Story = {};
export const DesktopSplit: Story = { parameters: { readerWidth: 900 } };
