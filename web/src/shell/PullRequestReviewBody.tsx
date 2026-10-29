import { createContext, isValidElement, useContext, useMemo, type ComponentProps } from "react";
import type { FileDiffMetadata } from "@pierre/diffs";
import { ChatCodeBlockPre, MessageResponse } from "@/components/ai-elements/message";
import { reviewSuggestionSource, type ReviewThread } from "@/lib/pullRequestReviewThreads";
import { PULL_REQUEST_MARKDOWN_COMPONENTS } from "./PullRequestDetails";

interface ReviewCodeContext {
  thread: ReviewThread;
  fileDiff?: FileDiffMetadata;
}

const ReviewCode = createContext<ReviewCodeContext | null>(null);

function ReviewCodeBlock({ children }: ComponentProps<"pre">) {
  const context = useContext(ReviewCode);
  if (
    !context ||
    !isValidElement<ComponentProps<"code">>(children) ||
    children.props.className !== "language-suggestion" ||
    (children.props.children != null && typeof children.props.children !== "string")
  )
    return <ChatCodeBlockPre>{children}</ChatCodeBlockPre>;

  const source = reviewSuggestionSource(context.thread, context.fileDiff);
  // Markdown adds one terminating newline to fenced code, including an empty suggestion.
  const replacement = (children.props.children ?? "").replace(/\r?\n$/, "");
  const added = replacement === "" ? [] : replacement.split(/\r?\n/);
  return (
    <section
      aria-label="Suggested change"
      className="my-2 min-w-0 overflow-hidden rounded-md border"
    >
      <div className="flex flex-wrap items-center gap-2 border-b bg-muted/30 px-3 py-2 font-sans text-ui text-muted-foreground">
        <span>Suggested change</span>
        {source?.original && <span className="text-xs">Original context</span>}
      </div>
      {source ? (
        <div className="max-h-80 overflow-auto">
          <table
            aria-label="Suggested change diff"
            className="w-full border-collapse text-left font-mono text-xs"
          >
            <tbody>
              {[
                { kind: "removed", lines: source.lines, sign: "−", color: "bg-red-500/10" },
                { kind: "added", lines: added, sign: "+", color: "bg-green-500/10" },
              ].flatMap(({ kind, lines, sign, color }) =>
                lines.map((text, index) => {
                  const lineNumber = source.line + index;
                  return (
                    <tr key={`${kind}:${lineNumber}`} data-suggestion-line={kind} className={color}>
                      <td className="w-1 select-none px-2 py-0.5 text-right align-top text-muted-foreground">
                        {lineNumber}
                      </td>
                      <td
                        className="w-1 select-none px-2 py-0.5 align-top"
                        aria-label={kind === "added" ? "Added" : "Removed"}
                      >
                        {sign}
                      </td>
                      <td className="px-2 py-0.5">
                        <pre className="m-0 whitespace-pre font-mono">{text || "\n"}</pre>
                      </td>
                    </tr>
                  );
                }),
              )}
            </tbody>
          </table>
        </div>
      ) : (
        <>
          <p className="px-3 py-2 text-xs text-muted-foreground">
            Original lines are unavailable for comparison.
          </p>
          <pre className="m-0 max-h-80 overflow-auto px-3 pb-3 font-mono text-xs whitespace-pre">
            {replacement || "(Empty replacement)"}
          </pre>
        </>
      )}
    </section>
  );
}

const REVIEW_MARKDOWN_COMPONENTS = { ...PULL_REQUEST_MARKDOWN_COMPONENTS, pre: ReviewCodeBlock };

export function PullRequestReviewBody({
  body,
  thread,
  fileDiff,
}: ReviewCodeContext & { body: string }) {
  const context = useMemo(() => ({ thread, fileDiff }), [thread, fileDiff]);
  return (
    <ReviewCode.Provider value={context}>
      <MessageResponse
        mode="static"
        allowGitHubAttachmentImages
        components={REVIEW_MARKDOWN_COMPONENTS}
      >
        {body}
      </MessageResponse>
    </ReviewCode.Provider>
  );
}
