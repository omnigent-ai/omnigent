// Tests for the shared image lightbox: a ZoomableImage renders a button around
// an <img>; activating it opens a full-screen Dialog showing the same source,
// which closes via Escape, the "x" button, or a click on the empty stage
// around the image.

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

const { authenticatedFetch, isAndroidShell, isIOSShell, toastError } = vi.hoisted(() => ({
  authenticatedFetch: vi.fn(),
  isAndroidShell: vi.fn(() => false),
  isIOSShell: vi.fn(() => false),
  toastError: vi.fn(),
}));

// getEmbedRoot decides the Radix portal container; null → portal to body.
vi.mock("@/lib/host", () => ({
  getEmbedRoot: () => null,
  isDatabricksWorkspace: () => false,
}));
vi.mock("@/lib/nativeBridge", () => ({
  isIOSShell: () => isIOSShell(),
  isAndroidShell: () => isAndroidShell(),
}));
vi.mock("@/lib/identity", () => ({
  authenticatedFetch: (url: string) => authenticatedFetch(url),
}));
vi.mock("sonner", () => ({ toast: { error: toastError } }));

import { captureDownloads } from "@/test/downloadTestHelpers";
import { ImageLightboxProvider, ZoomableImage } from "./ImageLightbox";

afterEach(cleanup);

function renderWithProvider() {
  return render(
    <ImageLightboxProvider>
      <ZoomableImage src="/pic.png" alt="diagram" className="size-10" />
    </ImageLightboxProvider>,
  );
}

describe("ZoomableImage + ImageLightboxProvider", () => {
  it("renders a button wrapping an image (keeps the img role/name)", () => {
    renderWithProvider();
    expect(screen.getByRole("button", { name: "Zoom image: diagram" })).toBeInTheDocument();
    // The inner <img> keeps its image role and alt-derived name.
    const img = screen.getByRole("img", { name: "diagram" });
    expect(img).toHaveAttribute("src", "/pic.png");
    expect(img).toHaveClass("size-10");
  });

  it("does not render a dialog until the image is activated", () => {
    renderWithProvider();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("opens a dialog showing the full image on click", () => {
    renderWithProvider();
    fireEvent.click(screen.getByRole("button", { name: "Zoom image: diagram" }));
    const dialog = screen.getByRole("dialog");
    // The dialog hosts its own copy of the image at the same source.
    const dialogImg = screen.getAllByRole("img", { name: "diagram" }).at(-1)!;
    expect(dialog).toContainElement(dialogImg);
    expect(dialogImg).toHaveAttribute("src", "/pic.png");
  });

  it("opens the full image from a focused thumbnail with Enter", async () => {
    renderWithProvider();
    screen.getByRole("button", { name: "Zoom image: diagram" }).focus();

    await userEvent.keyboard("{Enter}");

    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getAllByRole("img", { name: "diagram" }).at(-1)).toHaveAttribute(
      "src",
      "/pic.png",
    );
  });

  it("closes the dialog with Escape", () => {
    renderWithProvider();
    fireEvent.click(screen.getByRole("button", { name: "Zoom image: diagram" }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("closes the dialog with the x button", () => {
    renderWithProvider();
    fireEvent.click(screen.getByRole("button", { name: "Zoom image: diagram" }));
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("zooms in and out via the toolbar, updating the scale and label", () => {
    renderWithProvider();
    fireEvent.click(screen.getByRole("button", { name: "Zoom image: diagram" }));

    // Starts at fit (100%); zoom-out disabled, the preview img is unscaled.
    const previewImg = screen.getAllByRole("img", { name: "diagram" }).at(-1)!;
    expect(screen.getByRole("button", { name: "Reset zoom" })).toHaveTextContent("100%");
    expect(screen.getByRole("button", { name: "Zoom out" })).toBeDisabled();
    expect(previewImg).toHaveStyle({ transform: "translate(0px, 0px) scale(1)" });

    // One zoom-in step is +0.5 → 150%, and the transform scales up.
    fireEvent.click(screen.getByRole("button", { name: "Zoom in" }));
    expect(screen.getByRole("button", { name: "Reset zoom" })).toHaveTextContent("150%");
    expect(screen.getByRole("button", { name: "Zoom out" })).toBeEnabled();
    expect(previewImg).toHaveStyle({ transform: "translate(0px, 0px) scale(1.5)" });

    // The percentage acts as a reset back to fit.
    fireEvent.click(screen.getByRole("button", { name: "Reset zoom" }));
    expect(screen.getByRole("button", { name: "Reset zoom" })).toHaveTextContent("100%");
    expect(previewImg).toHaveStyle({ transform: "translate(0px, 0px) scale(1)" });
  });
});

describe("dismissing the lightbox by clicking outside the image", () => {
  /** Open the preview and return its image and the stage around it. */
  function openPreview() {
    renderWithProvider();
    fireEvent.click(screen.getByRole("button", { name: "Zoom image: diagram" }));
    const previewImg = screen.getAllByRole("img", { name: "diagram" }).at(-1)!;
    const stage = previewImg.parentElement!;
    // jsdom has no pointer capture; panning calls it on the stage.
    stage.setPointerCapture = vi.fn();
    stage.releasePointerCapture = vi.fn();
    return { previewImg, stage };
  }

  it("closes when the click lands on the stage, not the image", () => {
    const { stage } = openPreview();
    fireEvent.click(stage);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("stays open when the click lands on the image itself", () => {
    const { previewImg } = openPreview();
    fireEvent.click(previewImg);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("stays open when a toolbar button is clicked", () => {
    openPreview();
    fireEvent.click(screen.getByRole("button", { name: "Zoom in" }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("closes on a stage click while zoomed in", () => {
    const { stage } = openPreview();
    fireEvent.click(screen.getByRole("button", { name: "Zoom in" }));
    fireEvent.click(stage);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("stays open when a pan drag ends on the stage", () => {
    // Panning a zoomed image releases the pointer over the stage, which the
    // browser then reports as a click there. Dismissing on it would close the
    // preview every time someone repositions the image.
    const { stage } = openPreview();
    fireEvent.click(screen.getByRole("button", { name: "Zoom in" }));

    fireEvent.pointerDown(stage, { pointerId: 1, clientX: 100, clientY: 100 });
    fireEvent.pointerMove(stage, { pointerId: 1, clientX: 180, clientY: 140 });
    fireEvent.pointerUp(stage, { pointerId: 1, clientX: 180, clientY: 140 });
    fireEvent.click(stage);

    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("closes on a plain click that follows a pan", () => {
    // The drag suppression lasts for that gesture only.
    const { stage } = openPreview();
    fireEvent.click(screen.getByRole("button", { name: "Zoom in" }));

    fireEvent.pointerDown(stage, { pointerId: 1, clientX: 100, clientY: 100 });
    fireEvent.pointerMove(stage, { pointerId: 1, clientX: 180, clientY: 140 });
    fireEvent.pointerUp(stage, { pointerId: 1, clientX: 180, clientY: 140 });
    fireEvent.click(stage);

    fireEvent.pointerDown(stage, { pointerId: 2, clientX: 50, clientY: 50 });
    fireEvent.pointerUp(stage, { pointerId: 2, clientX: 50, clientY: 50 });
    fireEvent.click(stage);

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});

describe("pinch-to-zoom", () => {
  interface Finger {
    clientX: number;
    clientY: number;
  }
  const finger = (clientX: number, clientY = 0): Finger => ({ clientX, clientY });
  // The dialog's scroll lock reads `changedTouches` from every touch event.
  const fingers = (touches: Finger[], changedTouches = touches) => ({ touches, changedTouches });

  function openPreview() {
    renderWithProvider();
    fireEvent.click(screen.getByRole("button", { name: "Zoom image: diagram" }));
    const previewImg = screen.getAllByRole("img", { name: "diagram" }).at(-1)!;
    Object.defineProperties(previewImg, {
      offsetWidth: { value: 400 },
      offsetHeight: { value: 300 },
    });
    const stage = previewImg.parentElement!;
    stage.setPointerCapture = vi.fn();
    return { previewImg, stage };
  }

  function pinch(target: Element, from: [Finger, Finger], to: [Finger, Finger]) {
    fireEvent.touchStart(target, fingers(from));
    fireEvent.touchMove(target, fingers(to));
    fireEvent.touchEnd(target, fingers([], to));
  }

  afterEach(() => {
    isAndroidShell.mockReset().mockReturnValue(false);
    isIOSShell.mockReset().mockReturnValue(false);
  });

  it("zooms with the fingers' spread and back out to fit", () => {
    const { previewImg, stage } = openPreview();

    pinch(stage, [finger(-50), finger(50)], [finger(-100), finger(100)]);
    expect(screen.getByRole("button", { name: "Reset zoom" })).toHaveTextContent("200%");
    expect(previewImg).toHaveStyle({ transform: "translate(0px, 0px) scale(2)" });

    pinch(stage, [finger(-100), finger(100)], [finger(-20), finger(20)]);
    expect(screen.getByRole("button", { name: "Reset zoom" })).toHaveTextContent("100%");
    expect(previewImg).toHaveStyle({ transform: "translate(0px, 0px) scale(1)" });
  });

  it("keeps the point between the fingers in place", () => {
    // jsdom lays the stage out at 0x0, so its center is the origin.
    const { previewImg, stage } = openPreview();

    pinch(stage, [finger(100, 50), finger(140, 50)], [finger(80, 50), finger(160, 50)]);

    expect(previewImg).toHaveStyle({ transform: "translate(-120px, -50px) scale(2)" });
  });

  it("moves the image with the fingers as they pinch", () => {
    const { previewImg, stage } = openPreview();

    pinch(stage, [finger(100, 50), finger(140, 50)], [finger(70, 80), finger(150, 80)]);

    expect(previewImg).toHaveStyle({ transform: "translate(-130px, -20px) scale(2)" });
  });

  it("holds the image's nearest edge when the fingers start beside it", () => {
    const { previewImg, stage } = openPreview();

    pinch(stage, [finger(280, 50), finger(320, 50)], [finger(260, 50), finger(340, 50)]);

    expect(previewImg).toHaveStyle({ transform: "translate(-100px, -50px) scale(2)" });
  });

  it("stops zooming in at 800%", () => {
    const { previewImg, stage } = openPreview();

    pinch(stage, [finger(-10), finger(10)], [finger(-200), finger(200)]);

    expect(screen.getByRole("button", { name: "Reset zoom" })).toHaveTextContent("800%");
    expect(previewImg).toHaveStyle({ transform: "translate(0px, 0px) scale(8)" });
  });

  it("anchors a new pinch on the zoomed image after a cancelled one", () => {
    const { previewImg, stage } = openPreview();
    fireEvent.touchStart(stage, fingers([finger(-50), finger(50)]));
    fireEvent.touchMove(stage, fingers([finger(-100), finger(100)]));
    fireEvent.touchCancel(stage, fingers([], [finger(-100), finger(100)]));

    pinch(stage, [finger(100, 50), finger(140, 50)], [finger(60, 50), finger(180, 50)]);

    expect(previewImg).toHaveStyle({ transform: "translate(-240px, -100px) scale(6)" });
  });

  it("stops a one-finger pan when a second finger lands", () => {
    const { previewImg, stage } = openPreview();
    fireEvent.click(screen.getByRole("button", { name: "Zoom in" }));

    fireEvent.pointerDown(stage, { pointerId: 1, clientX: 0, clientY: 0 });
    fireEvent.pointerMove(stage, { pointerId: 1, clientX: 0, clientY: 20 });
    expect(previewImg).toHaveStyle({ transform: "translate(0px, 20px) scale(1.5)" });

    fireEvent.pointerDown(stage, { pointerId: 2, clientX: 100, clientY: 20 });
    fireEvent.touchStart(stage, fingers([finger(0, 20), finger(100, 20)]));
    fireEvent.pointerMove(stage, { pointerId: 2, clientX: 100, clientY: 100 });

    expect(previewImg).toHaveStyle({ transform: "translate(0px, 20px) scale(1.5)" });
  });

  it("stays open when the pinch's fingers lift, then closes on a tap beside the image", () => {
    const { stage } = openPreview();
    pinch(stage, [finger(-50), finger(50)], [finger(-100), finger(100)]);

    fireEvent.click(stage);
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    fireEvent.pointerDown(stage, { pointerId: 3, clientX: 0, clientY: 0 });
    fireEvent.click(stage);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it.each([
    ["iOS", isIOSShell],
    ["Android", isAndroidShell],
  ])(
    "opens a chat image's preview once two fingers spread a fifth wider in the %s shell",
    (_, shell) => {
      shell.mockReturnValue(true);
      renderWithProvider();
      const thumbnail = screen.getByRole("button", { name: "Zoom image: diagram" });

      fireEvent.touchStart(thumbnail, fingers([finger(-10), finger(10)]));
      expect(fireEvent.touchMove(thumbnail, fingers([finger(-11.9), finger(11.9)]))).toBe(false);
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

      expect(fireEvent.touchMove(thumbnail, fingers([finger(-12), finger(12)]))).toBe(false);
      expect(screen.getByRole("dialog")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Reset zoom" })).toHaveTextContent("100%");

      fireEvent.touchMove(thumbnail, fingers([finger(-24), finger(24)]));
      expect(screen.getByRole("button", { name: "Reset zoom" })).toHaveTextContent("200%");
    },
  );

  it("leaves a pinch on a chat image to the page outside the native shells", () => {
    renderWithProvider();
    const thumbnail = screen.getByRole("button", { name: "Zoom image: diagram" });

    fireEvent.touchStart(thumbnail, fingers([finger(-10), finger(10)]));
    const move = fireEvent.touchMove(thumbnail, fingers([finger(-30), finger(30)]));

    expect(move).toBe(true);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});

describe("downloading from the lightbox", () => {
  function openImage(props: {
    src: string;
    alt?: string;
    downloadName?: string;
    downloadPath?: string;
    downloadBlob?: Blob;
  }) {
    render(
      <ImageLightboxProvider>
        <ZoomableImage alt="diagram" {...props} />
      </ImageLightboxProvider>,
    );
    fireEvent.click(screen.getByRole("button", { name: /^Zoom image/ }));
    return within(screen.getByRole("dialog"));
  }

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    authenticatedFetch.mockReset();
    isAndroidShell.mockReset().mockReturnValue(false);
    isIOSShell.mockReset().mockReturnValue(false);
    toastError.mockReset();
  });

  it("offers no download in the iOS shell, whose web view can't save one", () => {
    isIOSShell.mockReturnValue(true);
    render(
      <ImageLightboxProvider>
        <ZoomableImage src="blob:shown-image" alt="diagram" downloadName="shot.png" />
      </ImageLightboxProvider>,
    );
    expect(screen.queryByRole("button", { name: /Download/ })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /^Zoom image/ }));

    expect(
      within(screen.getByRole("dialog")).queryByRole("button", { name: /Download/ }),
    ).toBeNull();
  });

  it("saves the bytes the preview already shows under the image's file name", () => {
    const clicked = captureDownloads();
    const dialog = openImage({ src: "blob:shown-image", downloadName: "shot.png" });

    fireEvent.click(dialog.getByRole("button", { name: "Download image: shot.png" }));

    expect(clicked).toEqual([["blob:shown-image", "shot.png"]]);
    expect(authenticatedFetch).not.toHaveBeenCalled();
  });

  it("names an inline data image after its alt text, typed by its MIME type", () => {
    const clicked = captureDownloads();
    const dialog = openImage({ src: "data:image/png;base64,iVBORw0KGgo=" });

    fireEvent.click(dialog.getByRole("button", { name: "Download image: diagram" }));

    expect(clicked).toEqual([["data:image/png;base64,iVBORw0KGgo=", "diagram.png"]]);
  });

  it("falls back to a typed generic name for an image with no alt text", () => {
    const clicked = captureDownloads();
    const dialog = openImage({ src: "data:image/jpeg;base64,/9j/4AAQ", alt: "" });

    fireEvent.click(dialog.getByRole("button", { name: "Download image" }));

    expect(clicked).toEqual([["data:image/jpeg;base64,/9j/4AAQ", "image.jpg"]]);
  });

  it("saves held bytes even after the source's object URL is gone", () => {
    const clicked = captureDownloads();
    const blob = new Blob(["png"], { type: "image/png" });
    const createObjectURL = vi.fn(() => "blob:lightbox-copy");
    vi.stubGlobal("URL", { createObjectURL, revokeObjectURL: vi.fn() });
    const dialog = openImage({ src: "blob:revoked", downloadName: "1f3a", downloadBlob: blob });

    fireEvent.click(dialog.getByRole("button", { name: "Download image: 1f3a" }));

    expect(createObjectURL).toHaveBeenCalledWith(blob);
    expect(clicked).toEqual([["blob:lightbox-copy", "1f3a.png"]]);
  });

  it("sends one request when Download is clicked again while saving", async () => {
    isAndroidShell.mockReturnValue(true);
    let respond: (response: Response) => void = () => {};
    authenticatedFetch.mockReturnValue(
      new Promise<Response>((resolve) => {
        respond = resolve;
      }),
    );
    const dialog = openImage({
      src: "/v1/sessions/s/resources/files/f/content",
      downloadName: "shot.png",
      downloadPath: "/v1/sessions/s/resources/files/f/content",
    });
    const download = dialog.getByRole("button", { name: "Download image: shot.png" });

    fireEvent.click(download);
    fireEvent.click(download);
    expect(authenticatedFetch).toHaveBeenCalledOnce();

    respond(new Response(null, { status: 503 }));
    await waitFor(() => expect(download).toBeEnabled());
  });

  it("keeps the hidden inline button from taking taps until it shows", () => {
    render(<ZoomableImage src="blob:shown" alt="diagram" downloadName="shot.png" />);

    // Touch screens never match hover, so the zoom button gets the tap.
    expect(screen.getByRole("button", { name: "Download image: shot.png" })).toHaveClass(
      "pointer-events-none",
      "group-hover/image:pointer-events-auto",
      "focus-visible:pointer-events-auto",
    );
  });

  it("saves a same-origin image from the API path it was read from", () => {
    const clicked = captureDownloads();
    const dialog = openImage({
      src: "/v1/sessions/s/resources/files/f/content",
      downloadName: "shot.png",
      downloadPath: "/v1/sessions/s/resources/files/f/content",
    });

    fireEvent.click(dialog.getByRole("button", { name: "Download image: shot.png" }));

    expect(clicked).toEqual([["/v1/sessions/s/resources/files/f/content", "shot.png"]]);
  });

  it("offers no download for a source it would have to fetch from elsewhere", () => {
    const dialog = openImage({ src: "https://example.com/remote.png", downloadName: "remote.png" });

    expect(dialog.queryByRole("button", { name: /Download/ })).toBeNull();
  });

  it("reports a failed download once the response arrives", async () => {
    // The Android shell can't save an http(s) link inside the WebView's
    // session, so the bytes come through the app's transport and can fail.
    isAndroidShell.mockReturnValue(true);
    let respond: (response: Response) => void = () => {};
    authenticatedFetch.mockReturnValue(
      new Promise<Response>((resolve) => {
        respond = resolve;
      }),
    );
    const dialog = openImage({
      src: "/v1/sessions/s/resources/files/f/content",
      downloadName: "shot.png",
      downloadPath: "/v1/sessions/s/resources/files/f/content",
    });

    fireEvent.click(dialog.getByRole("button", { name: "Download image: shot.png" }));
    expect(authenticatedFetch).toHaveBeenCalledWith("/v1/sessions/s/resources/files/f/content");
    expect(toastError).not.toHaveBeenCalled();

    respond(new Response(null, { status: 503, statusText: "Service Unavailable" }));
    await waitFor(() => expect(toastError).toHaveBeenCalledWith("Download failed"));
  });
});
