import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { macAccountChanged, resetDesktopReadPathForTests } from "@/lib/desktopReadPath";
import type * as IdentityModule from "@/lib/identity";
import { macReadScopeKey } from "@/lib/macReadScope";
import { useMacReadScopes } from "./useMacReadScopes";
import { EXAMPLE_SPOKE_HOST, withPullRequestIntegrations } from "@/test/pullRequestIntegrations";

// GitSpoke stand-in hosts are recognized by name, as a build that registers GitSpoke does.
withPullRequestIntegrations({ namedCodeHosts: [EXAMPLE_SPOKE_HOST] });

vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => "user-1",
}));

const SPOKE = "2.spoke.example.com";

function Reader({ read }: { read: () => Promise<string> }) {
  useMacReadScopes();
  const { data } = useQuery({
    queryKey: ["reader", ...macReadScopeKey("gitspoke", SPOKE)],
    queryFn: read,
    staleTime: 30_000,
  });
  return <p>{data ?? "loading"}</p>;
}

afterEach(() => resetDesktopReadPathForTests());

describe("useMacReadScopes", () => {
  it("drops what a scope cached when its identity changes while no reader is mounted", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const view = render(
      <QueryClientProvider client={client}>
        <Reader read={async () => "as alice"} />
      </QueryClientProvider>,
    );
    expect(await screen.findByText("as alice")).toBeVisible();
    view.unmount();

    macAccountChanged("gitspoke", SPOKE);

    // Bob's read is still on its way; Alice's cached answer must not show meanwhile.
    render(
      <QueryClientProvider client={client}>
        <Reader read={() => new Promise<string>(() => {})} />
      </QueryClientProvider>,
    );
    expect(screen.getByText("loading")).toBeVisible();
    expect(screen.queryByText("as alice")).toBeNull();
  });
});
