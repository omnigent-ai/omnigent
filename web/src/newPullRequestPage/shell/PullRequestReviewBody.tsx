import { createContext, isValidElement, useContext, useMemo, type ComponentProps } from "react";
import { DIFF_PALETTE_CSS, DIFF_SYNTAX_THEME } from "@/lib/diffTheme";
import type { FileDiffMetadata } from "@pierre/diffs";
import { FileDiff } from "@pierre/diffs/react";
import { ChatCodeBlockPre, MessageResponse } from "@/components/ai-elements/message";
import { useResolvedThemeMode } from "@/components/theme/useResolvedThemeMode";
import {
  reviewSuggestionDiff,
  reviewSuggestionSource,
  type ReviewThread,
} from "@/lib/pullRequestReviewThreads";
import { LIGHTBOX_GALLERY_PROPS } from "@/lib/lightboxGallery";
import { PULL_REQUEST_MARKDOWN_COMPONENTS } from "./PullRequestDetails";
import { GITHUB_MARKDOWN_REHYPE_PLUGINS } from "@/lib/githubMarkdownPlugins";

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

  // Markdown adds one terminating newline to fenced code, including an empty suggestion.
  const replacement = (children.props.children ?? "").replace(/\r?\n$/, "");
  return <ReviewSuggestion {...context} replacement={replacement} />;
}

function ReviewSuggestion({
  thread,
  fileDiff,
  replacement,
}: ReviewCodeContext & { replacement: string }) {
  const themeType = useResolvedThemeMode();
  const source = useMemo(() => reviewSuggestionSource(thread, fileDiff), [thread, fileDiff]);
  const diff = useMemo(
    () => source && reviewSuggestionDiff(thread.path, source, replacement),
    [thread.path, source, replacement],
  );
  const options = useMemo(
    () => ({
      theme: DIFF_SYNTAX_THEME,
      themeType,
      diffStyle: "unified" as const,
      diffIndicators: "classic" as const,
      disableFileHeader: true,
      unsafeCSS: DIFF_PALETTE_CSS,
    }),
    [themeType],
  );
  return (
    <section
      aria-label="Suggested change"
      className="my-2 min-w-0 overflow-hidden rounded-md border"
    >
      <div className="flex flex-wrap items-center gap-2 border-b bg-muted/30 px-3 py-2 font-sans text-ui text-muted-foreground">
        <span>Suggested change</span>
        {source?.original && <span className="text-xs">Original context</span>}
      </div>
      {diff ? (
        <div className="max-h-80 overflow-auto">
          <FileDiff fileDiff={diff} options={options} disableWorkerPool />
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
      <div {...LIGHTBOX_GALLERY_PROPS} className="contents">
        <MessageResponse
          mode="static"
          allowGitHubAttachmentImages
          rehypePluginsAfterSanitizing={GITHUB_MARKDOWN_REHYPE_PLUGINS}
          components={REVIEW_MARKDOWN_COMPONENTS}
        >
          {body}
        </MessageResponse>
      </div>
    </ReviewCode.Provider>
  );
}
