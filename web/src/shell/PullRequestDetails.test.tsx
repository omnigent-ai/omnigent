// PullRequestTitle and PullRequestDescription against the real details hook, with
// fetch stubbed: edits post what the editor shows and settle to the host's answer,
// a draft outlives the Summary tab unmounting, and pasted media upload into the draft.

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { afterEach, expect, it, onTestFinished, vi } from "vitest";
import type { PullRequestAttachment, PullRequestInfo } from "@/hooks/usePullRequests";
import {
  attachmentMarkdown,
  insertOnOwnLine,
  PullRequestDescription,
  PullRequestTitle,
  rememberUploadedImage,
  resetPullRequestDescriptionDrafts,
} from "./PullRequestDetails";

const PR_URL = "https://github.com/acme/app/pull/7";
const INFO_KEY = ["github-info", "conv_1", PR_URL];
const DETAILS_PATH = "/v1/sessions/conv_1/resources/github/details";
const ATTACHMENTS_PATH = "/v1/sessions/conv_1/resources/github/attachments";
const ASSET_URL = "https://github.com/user-attachments/assets/0f1e2d3c-aaaa-bbbb-cccc-1234567890ab";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  resetPullRequestDescriptionDrafts();
});

interface PendingRequest<T> {
  body: T;
  respond: (response: Response) => void;
}

/** Stub fetch: details posts and uploads wait for the test to answer. */
function stubFetch() {
  const edits: PendingRequest<Record<string, unknown>>[] = [];
  const uploads: PendingRequest<FormData>[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const { pathname } = new URL(String(input), "http://localhost");
      return new Promise<Response>((respond) => {
        if (pathname === DETAILS_PATH)
          edits.push({ body: JSON.parse(String(init?.body)), respond });
        else if (pathname === ATTACHMENTS_PATH)
          uploads.push({ body: init?.body as FormData, respond });
        else respond(new Response("{}", { status: 404 }));
      });
    }),
  );
  return { edits, uploads };
}

/** Stub jsdom's missing object URLs, numbering each copy. */
function stubObjectUrls() {
  const created: Blob[] = [];
  const revoked: string[] = [];
  const original = { createObjectURL: URL.createObjectURL, revokeObjectURL: URL.revokeObjectURL };
  Object.assign(URL, {
    createObjectURL: (blob: Blob) => `blob:copy-${created.push(blob)}`,
    revokeObjectURL: (url: string) => void revoked.push(url),
  });
  onTestFinished(() => void Object.assign(URL, original));
  return { created, revoked };
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status });
}

function details(title: string, body: string): Response {
  return json({ object: "session.github.pr_details", title, body });
}

function attachment(contentType: string): Response {
  return json({
    object: "session.github.pr_attachment",
    url: ASSET_URL,
    name: "shot.png",
    content_type: contentType,
  } satisfies PullRequestAttachment);
}

/** The title and description fed from the info cache, as the panel feeds them. */
function CachedDetails({ editable = true, attachments = true }) {
  const { data } = useQuery<PullRequestInfo>({
    queryKey: INFO_KEY,
    queryFn: () => new Promise(() => {}),
    enabled: false,
  });
  return (
    <>
      <PullRequestTitle
        conversationId="conv_1"
        prUrl={PR_URL}
        url={PR_URL}
        title={data?.pr?.title ?? ""}
        numberLabel="#7"
        editable={editable}
      />
      <PullRequestDescription
        conversationId="conv_1"
        prUrl={PR_URL}
        body={data?.pr?.body}
        editable={editable}
        attachments={attachments}
      />
    </>
  );
}

function renderDetails(props: { editable?: boolean; attachments?: boolean } = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  client.setQueryData<PullRequestInfo>(INFO_KEY, {
    object: "session.github.info",
    available: true,
    selected_pr_url: PR_URL,
    pr: {
      number: 7,
      title: "Old title",
      state: "OPEN",
      url: PR_URL,
      is_draft: false,
      author: "dev",
      base_ref: "main",
      head_ref: "details",
      checks: { passing: 0, failing: 0, pending: 0, total: 0, runs: [] },
      body: "Old body",
    },
  });
  const view = render(
    <QueryClientProvider client={client}>
      <CachedDetails {...props} />
    </QueryClientProvider>,
  );
  return { client, view };
}

function editor(): HTMLTextAreaElement {
  return screen.getByRole("textbox", { name: "Pull request description" }) as HTMLTextAreaElement;
}

it("renames the PR and shows the title GitHub reports", async () => {
  const { edits } = stubFetch();
  const { client } = renderDetails();

  fireEvent.click(screen.getByRole("button", { name: "Edit title" }));
  const input = screen.getByRole("textbox", { name: "Pull request title" });
  expect(input).toHaveValue("Old title");
  fireEvent.change(input, { target: { value: "  New title " } });
  fireEvent.submit(input.closest("form")!);

  await waitFor(() => expect(edits).toHaveLength(1));
  expect(edits[0].body).toEqual({ pr_url: PR_URL, title: "New title" });
  expect(screen.getByRole("button", { name: /Saving/ })).toBeDisabled();

  await act(async () => edits[0].respond(details("New title", "Old body")));

  expect(await screen.findByRole("link", { name: /New title/ })).toHaveAttribute("href", PR_URL);
  expect(client.getQueryData<PullRequestInfo>(INFO_KEY)?.pr?.title).toBe("New title");
});

it("keeps the title draft and shows GitHub's refusal", async () => {
  const { edits } = stubFetch();
  renderDetails();

  fireEvent.click(screen.getByRole("button", { name: "Edit title" }));
  const input = screen.getByRole("textbox", { name: "Pull request title" });
  fireEvent.change(input, { target: { value: "Renamed" } });
  fireEvent.submit(input.closest("form")!);
  await waitFor(() => expect(edits).toHaveLength(1));
  await act(async () =>
    edits[0].respond(
      json({ detail: "The GitHub account on the host cannot edit this pull request." }, 400),
    ),
  );

  expect(await screen.findByRole("alert")).toHaveTextContent("cannot edit this pull request");
  expect(screen.getByRole("textbox", { name: "Pull request title" })).toHaveValue("Renamed");

  fireEvent.keyDown(screen.getByRole("textbox", { name: "Pull request title" }), { key: "Escape" });
  expect(screen.getByRole("link", { name: /Old title/ })).toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("an unchanged title closes without asking GitHub", () => {
  const { edits } = stubFetch();
  renderDetails();

  fireEvent.click(screen.getByRole("button", { name: "Edit title" }));
  fireEvent.submit(screen.getByRole("textbox", { name: "Pull request title" }).closest("form")!);

  expect(screen.getByRole("link", { name: /Old title/ })).toBeInTheDocument();
  expect(edits).toHaveLength(0);
});

it("read-only viewers get no edit controls", () => {
  stubFetch();
  renderDetails({ editable: false });

  expect(screen.queryByRole("button", { name: "Edit title" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Edit description" })).not.toBeInTheDocument();
  expect(screen.getByText("Old body")).toBeInTheDocument();
});

it("saves the description verbatim and closes the editor", async () => {
  const { edits } = stubFetch();
  const { client } = renderDetails();

  fireEvent.click(screen.getByRole("button", { name: "Edit description" }));
  expect(editor()).toHaveValue("Old body");
  fireEvent.change(editor(), { target: { value: "## Summary\n<!-- kept -->\n" } });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));

  await waitFor(() => expect(edits).toHaveLength(1));
  expect(edits[0].body).toEqual({ pr_url: PR_URL, body: "## Summary\n<!-- kept -->\n" });
  await act(async () => edits[0].respond(details("Old title", "New body")));

  // The cache feeds the rendered body (Streamdown only re-renders new text in a browser).
  await waitFor(() =>
    expect(client.getQueryData<PullRequestInfo>(INFO_KEY)?.pr?.body).toBe("New body"),
  );
  expect(
    screen.queryByRole("textbox", { name: "Pull request description" }),
  ).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Edit description" })).toBeInTheDocument();
});

it("scrolls the opened editor into view, where scrolling returns a Promise", () => {
  stubFetch();
  // jsdom has no scrollIntoView; newer browsers' resolves a Promise.
  const scrollIntoView = vi.fn(() => Promise.resolve());
  Object.assign(Element.prototype, { scrollIntoView });
  onTestFinished(() => void delete (Element.prototype as Partial<Element>).scrollIntoView);
  const { view } = renderDetails();

  fireEvent.click(screen.getByRole("button", { name: "Edit description" }));
  expect(scrollIntoView).toHaveBeenCalledWith({ block: "nearest" });
  view.unmount();
});

it("keeps the description draft when the Summary tab unmounts", () => {
  stubFetch();
  const { view } = renderDetails();

  fireEvent.click(screen.getByRole("button", { name: "Edit description" }));
  fireEvent.change(editor(), { target: { value: "Half-written" } });
  const tree = (
    <QueryClientProvider client={new QueryClient()}>
      <PullRequestDescription
        conversationId="conv_1"
        prUrl={PR_URL}
        body="Old body"
        editable
        attachments
      />
    </QueryClientProvider>
  );
  view.unmount();
  render(tree);

  expect(editor()).toHaveValue("Half-written");
});

it("asks before discarding description changes", () => {
  stubFetch();
  const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
  renderDetails();

  fireEvent.click(screen.getByRole("button", { name: "Edit description" }));
  fireEvent.change(editor(), { target: { value: "Changed" } });
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(editor()).toHaveValue("Changed");
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

  expect(confirm).toHaveBeenCalledTimes(2);
  expect(
    screen.queryByRole("textbox", { name: "Pull request description" }),
  ).not.toBeInTheDocument();
  expect(screen.getByText("Old body")).toBeInTheDocument();
});

it("uploads a pasted image at the caret and embeds GitHub's URL", async () => {
  const { uploads } = stubFetch();
  renderDetails();

  fireEvent.click(screen.getByRole("button", { name: "Edit description" }));
  fireEvent.change(editor(), { target: { value: "Before After" } });
  editor().setSelectionRange(7, 7);
  fireEvent.select(editor());
  const image = new File(["png"], "login error.png", { type: "image/png" });
  fireEvent.paste(editor(), { clipboardData: { files: [image] } });

  expect(editor()).toHaveValue("Before \n![Uploading login error.png…](#upload-1)\nAfter");
  expect(screen.getByRole("status")).toHaveTextContent("Uploading 1 file…");
  expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  await waitFor(() => expect(uploads).toHaveLength(1));
  expect(uploads[0].body.get("pr_url")).toBe(PR_URL);
  expect((uploads[0].body.get("file") as File).name).toBe("login error.png");

  await act(async () => uploads[0].respond(attachment("image/png")));

  await waitFor(() => expect(editor()).toHaveValue(`Before \n![login error](${ASSET_URL})\nAfter`));
  expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
});

it("an upload from a discarded draft leaves the reopened draft alone", async () => {
  const { uploads } = stubFetch();
  vi.spyOn(window, "confirm").mockReturnValue(true);
  renderDetails();
  const screenshot = () => new File(["png"], "image.png", { type: "image/png" });

  fireEvent.click(screen.getByRole("button", { name: "Edit description" }));
  fireEvent.change(editor(), { target: { value: "" } });
  fireEvent.paste(editor(), { clipboardData: { files: [screenshot()] } });
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  fireEvent.click(screen.getByRole("button", { name: "Edit description" }));
  fireEvent.change(editor(), { target: { value: "" } });
  fireEvent.paste(editor(), { clipboardData: { files: [screenshot()] } });
  await waitFor(() => expect(uploads).toHaveLength(2));
  const pending = "![Uploading image.png…](#upload-2)";
  expect(editor()).toHaveValue(pending);

  await act(async () => uploads[0].respond(json({ detail: "The discarded upload failed" }, 400)));
  expect(editor()).toHaveValue(pending);
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();

  await act(async () => uploads[1].respond(attachment("image/png")));
  await waitFor(() => expect(editor()).toHaveValue(`![image](${ASSET_URL})`));
  expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
});

it("an upload lands in its draft after the editor remounts", async () => {
  const { uploads } = stubFetch();
  const { view } = renderDetails();

  fireEvent.click(screen.getByRole("button", { name: "Edit description" }));
  fireEvent.change(editor(), { target: { value: "" } });
  fireEvent.paste(editor(), {
    clipboardData: { files: [new File(["png"], "image.png", { type: "image/png" })] },
  });
  await waitFor(() => expect(uploads).toHaveLength(1));
  view.unmount();
  render(
    <QueryClientProvider client={new QueryClient()}>
      <PullRequestDescription
        conversationId="conv_1"
        prUrl={PR_URL}
        body="Old body"
        editable
        attachments
      />
    </QueryClientProvider>,
  );
  fireEvent.paste(editor(), {
    clipboardData: { files: [new File(["png"], "image.png", { type: "image/png" })] },
  });
  expect(editor()).toHaveValue(
    "![Uploading image.png…](#upload-1)\n![Uploading image.png…](#upload-2)",
  );

  await act(async () => uploads[0].respond(attachment("image/png")));
  await waitFor(() =>
    expect(editor()).toHaveValue(`![image](${ASSET_URL})\n![Uploading image.png…](#upload-2)`),
  );
});

it("shows this tab's uploads from local copies, before and after saving", async () => {
  const { uploads } = stubFetch();
  const copies = stubObjectUrls();
  renderDetails();

  fireEvent.click(screen.getByRole("button", { name: "Edit description" }));
  fireEvent.change(editor(), { target: { value: "" } });
  const image = new File(["png"], "shot.png", { type: "image/png" });
  fireEvent.paste(editor(), { clipboardData: { files: [image] } });
  await waitFor(() => expect(uploads).toHaveLength(1));
  await act(async () => uploads[0].respond(attachment("image/png")));
  await waitFor(() => expect(editor()).toHaveValue(`![shot](${ASSET_URL})`));
  fireEvent.mouseDown(screen.getByRole("tab", { name: "Preview" }));

  expect(await screen.findByRole("img", { name: "shot" })).toHaveAttribute("src", "blob:copy-1");
  expect(copies.created).toEqual([image]);

  // A saved body names the same URL, which GitHub serves only some seconds later.
  vi.spyOn(window, "confirm").mockReturnValue(true);
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  cleanup();
  render(
    <QueryClientProvider client={new QueryClient()}>
      <PullRequestDescription
        conversationId="conv_1"
        prUrl={PR_URL}
        body={`![shot](${ASSET_URL})`}
        editable
        attachments
      />
    </QueryClientProvider>,
  );
  const saved = await screen.findByRole("img", { name: "shot" });
  expect(saved).toHaveAttribute("src", "blob:copy-1");
  expect(saved).toHaveAttribute("referrerpolicy", "no-referrer");
  expect(copies.revoked).toEqual([]);
});

it("keeps no copies of videos and drops the oldest image copies past the cap", () => {
  const copies = stubObjectUrls();
  const video = new File(["mp4"], "demo.mp4", { type: "video/mp4" });
  rememberUploadedImage(`${ASSET_URL}-video`, video);
  expect(copies.created).toEqual([]);

  for (let index = 0; index <= 50; index += 1) {
    rememberUploadedImage(
      `${ASSET_URL}-${index}`,
      new File(["png"], "a.png", { type: "image/png" }),
    );
  }
  expect(copies.revoked).toEqual(["blob:copy-1"]);
});

it("falls back to GitHub, and says so when GitHub can't serve the image", () => {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <PullRequestDescription
        conversationId="conv_1"
        prUrl={PR_URL}
        body={`![other](${ASSET_URL})`}
        editable={false}
        attachments
      />
    </QueryClientProvider>,
  );
  const image = screen.getByRole("img", { name: "other" });
  expect(image).toHaveAttribute("src", ASSET_URL);

  fireEvent.error(image);
  expect(screen.getByText("Image not available")).toBeInTheDocument();
});

it("puts an uploaded video on its own line and drops a failed upload", async () => {
  const { uploads } = stubFetch();
  renderDetails();

  fireEvent.click(screen.getByRole("button", { name: "Edit description" }));
  fireEvent.change(editor(), { target: { value: "" } });
  const video = new File(["mp4"], "demo.mp4", { type: "video/mp4" });
  const image = new File(["png"], "shot.png", { type: "image/png" });
  fireEvent.drop(editor(), { dataTransfer: { files: [video, image], types: ["Files"] } });

  await waitFor(() => expect(uploads).toHaveLength(2));
  expect(screen.getByRole("status")).toHaveTextContent("Uploading 2 files…");
  await act(async () => uploads[0].respond(attachment("video/mp4")));
  await act(async () =>
    uploads[1].respond(json({ detail: "The GitHub account on the host needs push access" }, 400)),
  );

  await waitFor(() => expect(editor()).toHaveValue(`${ASSET_URL}\n`));
  expect(screen.getByRole("alert")).toHaveTextContent(
    "Couldn’t attach shot.png: The GitHub account",
  );
});

it("rejects other files and leaves text pastes alone", () => {
  const { uploads } = stubFetch();
  renderDetails();

  fireEvent.click(screen.getByRole("button", { name: "Edit description" }));
  const pdf = new File(["%PDF"], "spec.pdf", { type: "application/pdf" });
  fireEvent.paste(editor(), { clipboardData: { files: [pdf] } });
  expect(screen.getByRole("alert")).toHaveTextContent("Only images and videos can be attached.");

  const textPaste = fireEvent.paste(editor(), { clipboardData: { files: [] } });
  expect(textPaste).toBe(true);
  expect(uploads).toHaveLength(0);
  expect(editor()).toHaveValue("Old body");
});

it("refuses files over the size limits without uploading them", () => {
  const { uploads } = stubFetch();
  renderDetails();
  const sized = (name: string, type: string, mb: number) => {
    const file = new File(["x"], name, { type });
    Object.defineProperty(file, "size", { value: mb * 1024 * 1024 + 1 });
    return file;
  };

  fireEvent.click(screen.getByRole("button", { name: "Edit description" }));
  fireEvent.paste(editor(), { clipboardData: { files: [sized("big.png", "image/png", 10)] } });
  expect(screen.getByRole("alert")).toHaveTextContent(
    "Couldn’t attach big.png: an image can be at most 10 MB.",
  );
  fireEvent.paste(editor(), { clipboardData: { files: [sized("big.mp4", "video/mp4", 70)] } });
  expect(screen.getByRole("alert")).toHaveTextContent(
    "Couldn’t attach big.mp4: a video can be at most 70 MB.",
  );

  expect(uploads).toHaveLength(0);
  expect(editor()).toHaveValue("Old body");
});

it("offers no uploads where the provider can't take them", () => {
  const { uploads } = stubFetch();
  renderDetails({ attachments: false });

  fireEvent.click(screen.getByRole("button", { name: "Edit description" }));
  const image = new File(["png"], "shot.png", { type: "image/png" });
  const pasted = fireEvent.paste(editor(), { clipboardData: { files: [image] } });

  expect(pasted).toBe(true);
  expect(uploads).toHaveLength(0);
  expect(screen.queryByRole("button", { name: "Attach images or videos" })).not.toBeInTheDocument();
});

it("embeds images with their name as alt text and videos as bare URLs", () => {
  const image = {
    object: "session.github.pr_attachment",
    url: ASSET_URL,
    name: "a.png",
    content_type: "image/png",
  } as const;
  expect(attachmentMarkdown(image, "Screen [1].png")).toBe(`![Screen 1](${ASSET_URL})`);
  expect(attachmentMarkdown(image, "")).toBe(`![image](${ASSET_URL})`);
  expect(attachmentMarkdown({ ...image, content_type: "video/quicktime" }, "clip.mov")).toBe(
    ASSET_URL,
  );
});

it.each([
  ["", 0, 0, "X", 1],
  ["ab", 1, 1, "a\nX\nb", 4],
  ["a\nb", 2, 2, "a\nX\nb", 4],
  ["ab", 2, 2, "ab\nX", 4],
  ["a word b", 2, 6, "a \nX\n b", 5],
])("puts an attachment on its own line in %j at %i–%i", (body, start, end, expected, caret) => {
  expect(insertOnOwnLine(body, "X", start, end)).toEqual({ body: expected, caret });
});
