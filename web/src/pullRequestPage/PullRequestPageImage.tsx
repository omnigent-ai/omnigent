// A GitHub-hosted image in a PR description or comment on the new page. Where the session's host
// reads attachments, one loads through it as the host's account; elsewhere it loads directly,
// which works for public repositories, and otherwise links to the image.

import { ImageOff } from "lucide-react";
import {
  createContext,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import { githubAttachmentAssetId } from "@/components/ai-elements/streamdown-security";
import { ZoomableImage } from "@/components/ImageLightbox";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { parseCodeHostPullRequestUrl } from "@/lib/codeHosts";
import { pullRequestPageUrl } from "@/lib/pullRequestReadRoute";
import { cn } from "@/lib/utils";

import {
  forgetPullRequestImage,
  loadPullRequestImage,
  pullRequestImageScope,
  retainPullRequestImages,
  type PullRequestImageFailure,
} from "./pullRequestImages";
import { usePullRequestMethodServed } from "./pullRequestPageRoute";

/** The PR the new page shows and its session, for its Markdown images; null outside it. */
const PullRequestImageContext = createContext<{ prUrl: string; conversationId: string } | null>(
  null,
);

export function usePullRequestImagePage(): string | null {
  return useContext(PullRequestImageContext)?.prUrl ?? null;
}

/** The PR link a page's images are held by. */
function pullRequestImageKey(prUrl: string): string {
  const pageUrl = pullRequestPageUrl(prUrl) ?? prUrl;
  return parseCodeHostPullRequestUrl(pageUrl)?.url ?? pageUrl;
}

/** Holds the page's PR for its images, so they load; leaving releases them. */
export function PullRequestImageScope({
  conversationId = "",
  prUrl,
  children,
}: {
  /** The session whose host reads attachments; without one they load directly. */
  conversationId?: string;
  prUrl: string;
  children: ReactNode;
}) {
  // A layout effect holds the PR before any image's load effect runs.
  useLayoutEffect(() => retainPullRequestImages(pullRequestImageKey(prUrl)), [prUrl]);
  const value = useMemo(() => ({ prUrl, conversationId }), [prUrl, conversationId]);
  return (
    <PullRequestImageContext.Provider value={value}>{children}</PullRequestImageContext.Provider>
  );
}

const FAILURE_COPY: Record<PullRequestImageFailure, string> = {
  not_found: "Couldn't load this image.",
  expired: "GitHub's link to this image expired before it loaded.",
  too_large: "This image is too large to show here.",
  unsupported_type: "This image's format can't be shown here.",
  query_failed: "Couldn't load this image.",
};

type ImageState =
  | { key: string; kind: "loading" }
  | { key: string; kind: "loaded"; url: string }
  | { key: string; kind: "direct" }
  | { key: string; kind: "failed"; reason: PullRequestImageFailure };

export function PullRequestPageImage({
  prUrl,
  src,
  alt,
  className,
  imageProps,
}: {
  prUrl: string;
  src: string;
  alt: string;
  className?: string;
  imageProps: React.ComponentProps<"img">;
}) {
  const conversationId = useContext(PullRequestImageContext)?.conversationId ?? "";
  // Without the host's image read, an attachment loads directly and links out if it can't.
  const served = usePullRequestMethodServed(conversationId, "readPullRequestImage");
  const pageUrl = pullRequestImageKey(prUrl);
  const target = useMemo(
    () => (served && conversationId ? { conversationId, prUrl: pageUrl } : null),
    [served, conversationId, pageUrl],
  );
  const isAttachment = githubAttachmentAssetId(src) !== null;
  const scope = isAttachment ? pullRequestImageScope(target) : null;
  const [attempt, setAttempt] = useState(0);
  const key = `${scope ?? "direct"}\n${src}\n${attempt}`;
  const [state, setState] = useState<ImageState>({ key, kind: scope ? "loading" : "direct" });
  // A new scope, source or retry starts over; one that's gone never settles this image.
  const current: ImageState =
    state.key === key ? state : { key, kind: scope ? "loading" : "direct" };

  useEffect(() => {
    if (!scope || !target) return;
    let live = true;
    void loadPullRequestImage(target, src).then((result) => {
      if (!live) return;
      if (result.kind === "loaded") setState({ key, kind: "loaded", url: result.url });
      // Not a signed attachment: a public image, which loads directly.
      else if (result.reason === "not_found") setState({ key, kind: "direct" });
      else setState({ key, kind: "failed", reason: result.reason });
    });
    return () => {
      live = false;
    };
    // `target` follows `scope`, which names its PR and session.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  if (current.kind === "loaded" || current.kind === "direct") {
    return (
      <ZoomableImage
        {...imageProps}
        alt={alt}
        src={current.kind === "loaded" ? current.url : src}
        referrerPolicy="no-referrer"
        onError={() => {
          // A copy that won't show is dropped, so Try again reads it afresh.
          if (current.kind === "loaded") forgetPullRequestImage(target, src);
          setState({ key, kind: "failed", reason: "query_failed" });
        }}
        buttonClassName="my-4"
        className={cn("inline-block max-w-full rounded-lg", className)}
      />
    );
  }
  if (current.kind === "loading") {
    return (
      <span className="my-2 inline-flex items-center gap-2 rounded-lg border border-border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
        <Spinner className="size-3.5" /> Loading image…
      </span>
    );
  }

  const openOnGitHub = (
    <Button variant="link" size="xs" asChild>
      <a href={src} target="_blank" rel="noreferrer">
        {isAttachment ? "Open on GitHub" : "Open image"}
      </a>
    </Button>
  );
  let message: string;
  let actions: ReactNode;
  if (scope) {
    message = FAILURE_COPY[current.reason];
    actions = (
      <>
        <Button variant="link" size="xs" onClick={() => setAttempt((count) => count + 1)}>
          Try again
        </Button>
        {openOnGitHub}
      </>
    );
  } else {
    message = "This image can't be shown here.";
    actions = openOnGitHub;
  }
  return (
    <span className="my-2 inline-flex flex-wrap items-center gap-x-2 gap-y-1 rounded-lg border border-border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
      <ImageOff aria-hidden className="size-3.5 shrink-0" />
      <span>
        {alt ? <span className="sr-only">{`${alt}: `}</span> : null}
        {message}
      </span>
      {actions}
    </span>
  );
}
