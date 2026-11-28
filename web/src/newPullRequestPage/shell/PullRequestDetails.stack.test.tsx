import { cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";

import { EXAMPLE_STACK_FORMAT, withPullRequestIntegrations } from "@/test/pullRequestIntegrations";
import { parseStack } from "@/lib/pullRequestStacks/stackFormats";
import { pullRequestStackView } from "@/pullRequestPage/usePullRequestStack";

import { PullRequestDescription } from "./PullRequestDetails";

// Stack descriptions in an example format, which the page reads once a build registers it.
const STACK_FORMATS = [EXAMPLE_STACK_FORMAT];
withPullRequestIntegrations({ stackFormats: STACK_FORMATS });

const PR_URL = "https://github.com/acme/app/pull/101";
const SECTION = [
  "## Stacked changes",
  "- [stack/100](https://github.com/acme/app/pull/100)",
  `  - [**stack/101**](${PR_URL}) ← _this PR_`,
];
const VERIFIED = [...SECTION, "", "---------", "Adds the auth API."].join("\n");
const TOLERATED = [...SECTION, "", "---", "Adds the auth API."].join("\n");

function stackOf(body: string) {
  const parse = parseStack(STACK_FORMATS, body, {
    host: "github.com",
    repository: "acme/app",
    number: 101,
  });
  return parse && pullRequestStackView(parse, { kind: "unavailable" }, () => true);
}

function renderDescription(body: string, stack = stackOf(body)) {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>
        <PullRequestDescription
          conversationId="conv_1"
          prUrl={PR_URL}
          body={body}
          stack={stack}
          editable={false}
          attachments={false}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(cleanup);

describe("a stacked PR's description", () => {
  it("shows the stack card in place of a verified stack section, around the text as written", () => {
    renderDescription(`Read this first.\n\n${VERIFIED}`);
    expect(screen.getByRole("navigation", { name: "Stack" })).toBeInTheDocument();
    expect(screen.getByText("Read this first.")).toBeInTheDocument();
    expect(screen.getByText("Adds the auth API.")).toBeInTheDocument();
    expect(screen.queryByText(/Stacked changes/)).not.toBeInTheDocument();
  });

  it("shows a stack section that isn't in the usual format as written, without a card", () => {
    const stack = stackOf(TOLERATED);
    expect(stack?.parse.trust).toBe("tolerated");
    renderDescription(TOLERATED, stack);
    expect(screen.queryByRole("navigation", { name: "Stack" })).not.toBeInTheDocument();
    expect(screen.getByText(/Stacked changes/)).toBeInTheDocument();
  });

  it("shows GitHub's own stack above the description, which stays exactly as written", () => {
    const described = stackOf(VERIFIED)!;
    const native = pullRequestStackView(
      { ...described.parse, format: "github-native", trust: "native" },
      { kind: "unavailable" },
      () => true,
    );
    const { container } = renderDescription(VERIFIED, native);
    const card = screen.getByRole("navigation", { name: "Stack" });
    // The description's own stack section isn't stripped or spliced around the card.
    expect(screen.getByText(/Stacked changes/)).toBeInTheDocument();
    expect(screen.getByText("Adds the auth API.")).toBeInTheDocument();
    expect(
      card.compareDocumentPosition(screen.getByText(/Stacked changes/)) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(container.querySelectorAll('[data-pull-request-stack-card=""]')).toHaveLength(1);
  });

  it("shows GitHub's own stack for a PR without a description", () => {
    const described = stackOf(VERIFIED)!;
    const native = pullRequestStackView(
      { ...described.parse, format: "github-native", trust: "native" },
      { kind: "unavailable" },
      () => true,
    );
    renderDescription("", native);
    expect(screen.getByRole("navigation", { name: "Stack" })).toBeInTheDocument();
    expect(screen.getByText("No description provided.")).toBeInTheDocument();
  });

  it("shows the whole description where the page shows no stack", () => {
    renderDescription(VERIFIED, null);
    expect(screen.queryByRole("navigation", { name: "Stack" })).not.toBeInTheDocument();
    expect(screen.getByText(/Stacked changes/)).toBeInTheDocument();
  });
});
