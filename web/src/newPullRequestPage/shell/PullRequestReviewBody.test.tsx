// A review comment's body through the real renderer: GitHub's emoji shortcodes show as their
// glyphs, as on GitHub, while code keeps them as typed.

import { render, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ReviewThread } from "@/lib/pullRequestReviewThreads";
import { PullRequestReviewBody } from "./PullRequestReviewBody";

const THREAD = { id: "PRRT_1", path: "src/app.ts" } as unknown as ReviewThread;

describe("PullRequestReviewBody", () => {
  it("renders emoji shortcodes in prose and leaves code spans alone", async () => {
    const { container } = render(
      <PullRequestReviewBody thread={THREAD} body={"Nice :sparkles: but rename `:sparkles:`"} />,
    );
    await waitFor(() => expect(container).toHaveTextContent("Nice ✨ but rename"));
    expect(container.querySelector("code")).toHaveTextContent(":sparkles:");
  });
});
