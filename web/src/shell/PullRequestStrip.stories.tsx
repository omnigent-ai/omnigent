import { useState } from "react";
import type { Meta, StoryObj } from "@storybook/react-vite";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { PullRequest, PullRequestAssociation, PullRequestInfo } from "@/hooks/usePullRequests";
import { PullRequestStrip } from "./PullRequestStrip";

const CONVERSATION_ID = "conversation-story";

interface Fixture {
  number: number;
  title: string;
  state?: string;
  is_draft?: boolean;
  checks?: Partial<PullRequest["checks"]>;
  /** Leave the PR's details unloaded, as while its first fetch runs. */
  unloaded?: boolean;
}

const FIXTURES: Fixture[] = [
  {
    number: 11069,
    title: "Fix streaming response status updates",
    checks: { failing: 1, passing: 100 },
  },
  {
    number: 11070,
    title: "Improve empty-state visuals",
    state: "CLOSED",
    checks: { pending: 36, passing: 42 },
  },
  {
    number: 11071,
    title: "Update pull-request review flow",
    is_draft: true,
    checks: { failing: 3, passing: 74 },
  },
  {
    number: 11072,
    title: "Merge the sidebar grouping options",
    state: "MERGED",
    checks: { passing: 88 },
  },
  { number: 11073, title: "Prototype the multi-PR strip", unloaded: true },
];

function url(number: number): string {
  return `https://github.com/omnigent-ai/omnigent/pull/${number}`;
}

function association(fixture: Fixture): PullRequestAssociation {
  return {
    url: url(fixture.number),
    host: "github.com",
    repository: "omnigent-ai/omnigent",
    number: fixture.number,
    title: fixture.title,
    relationship: "created",
  };
}

/** A query client already holding each loaded fixture's info, so nothing fetches. */
function seededClient(fixtures: Fixture[]): QueryClient {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  for (const fixture of fixtures) {
    if (fixture.unloaded) continue;
    const checks = { passing: 0, failing: 0, pending: 0, runs: [], ...fixture.checks };
    const info: PullRequestInfo = {
      object: "session.github.info",
      available: true,
      pr: {
        number: fixture.number,
        title: fixture.title,
        state: fixture.state ?? "OPEN",
        url: url(fixture.number),
        is_draft: fixture.is_draft ?? false,
        author: "octocat",
        base_ref: "main",
        head_ref: "topic",
        checks: { ...checks, total: checks.passing + checks.failing + checks.pending },
      },
    };
    client.setQueryData(["github-info", CONVERSATION_ID, url(fixture.number)], info);
  }
  return client;
}

function StatefulStrip({ fixtures }: { fixtures: Fixture[] }) {
  const [client] = useState(() => seededClient(fixtures));
  const [linked, setLinked] = useState(fixtures);
  const [selected, setSelected] = useState(url(fixtures[0].number));
  return (
    <QueryClientProvider client={client}>
      <div className="w-[420px] rounded-lg border border-border bg-background">
        <PullRequestStrip
          conversationId={CONVERSATION_ID}
          prs={linked.map(association)}
          selectedUrl={selected}
          onSelect={setSelected}
          onUnlink={(prUrl) => setLinked(linked.filter((fixture) => url(fixture.number) !== prUrl))}
        />
      </div>
    </QueryClientProvider>
  );
}

const meta = {
  title: "Shell/PullRequestStrip",
  component: StatefulStrip,
} satisfies Meta<typeof StatefulStrip>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Overflowing: Story = {
  args: { fixtures: FIXTURES },
};

export const FitsWithoutChevrons: Story = {
  args: { fixtures: FIXTURES.slice(0, 2) },
};

export const SinglePullRequest: Story = {
  args: { fixtures: FIXTURES.slice(0, 1) },
};
