// jsdom's XMLHttpRequest would reach the network. This one sends through the global `fetch`, so
// a test's fetch mock answers uploads too. The whole body counts as sent once the fetch answers,
// unless a test reports progress itself with `progress()`, which then owns the progress events.

type Listener = ((event: ProgressEvent) => void) | null;

/** Every request sent through the double, newest last, for tests that drive progress by hand. */
export const sentXhrs: FetchBackedXhr[] = [];
const inFlight = new Set<FetchBackedXhr>();

/** Aborts every request still open, so a test that never answers doesn't leak into the next. */
export function abortInFlightXhrs(): void {
  for (const xhr of [...inFlight]) xhr.abort();
}

export class FetchBackedXhr {
  static readonly DONE = 4;
  readyState = 0;
  status = 0;
  statusText = "";
  responseType: XMLHttpRequestResponseType = "";
  response: unknown = null;
  withCredentials = false;
  readonly upload: { onprogress: Listener } = { onprogress: null };
  onload: Listener = null;
  onerror: Listener = null;
  onabort: Listener = null;
  method = "GET";
  url = "";
  readonly requestHeaders = new Headers();
  body: unknown = null;
  private responseHeaders = new Headers();
  private readonly controller = new AbortController();
  private finished = false;
  private reportedByHand = false;

  open(method: string, url: string): void {
    this.method = method;
    this.url = url;
    this.readyState = 1;
  }

  setRequestHeader(name: string, value: string): void {
    this.requestHeaders.set(name, value);
  }

  getAllResponseHeaders(): string {
    let lines = "";
    this.responseHeaders.forEach((value, name) => {
      lines += `${name}: ${value}\r\n`;
    });
    return lines;
  }

  /** Report `loaded` of `total` bytes sent, as the browser does while uploading. */
  progress(loaded: number, total: number): void {
    this.reportedByHand = true;
    this.upload.onprogress?.({ loaded, total, lengthComputable: true } as ProgressEvent);
  }

  send(body: unknown = null): void {
    this.body = body;
    sentXhrs.push(this);
    inFlight.add(this);
    void this.run(body, bodySize(body));
  }

  private async run(body: unknown, size: number): Promise<void> {
    let status: number;
    let statusText: string;
    let headers: Headers;
    let bytes: ArrayBuffer;
    try {
      const res = await globalThis.fetch(this.url, {
        method: this.method,
        headers: Object.fromEntries(this.requestHeaders.entries()),
        body: body as BodyInit | null,
        credentials: this.withCredentials ? "include" : "same-origin",
        signal: this.controller.signal,
      });
      if (this.finished) return;
      bytes = await responseBytes(res);
      if (this.finished) return;
      if (!this.reportedByHand) this.progress(size, size);
      status = res.status;
      statusText = res.statusText;
      headers = new Headers(res.headers);
    } catch {
      if (!this.finished) this.finish(this.onerror);
      return;
    }
    // Outside the `try`: an `onload` that throws must surface, not read as a network failure.
    this.status = status;
    this.statusText = statusText;
    this.responseHeaders = headers;
    this.response = this.responseType === "arraybuffer" ? bytes : new TextDecoder().decode(bytes);
    this.finish(this.onload);
  }

  abort(): void {
    if (this.finished) return;
    this.controller.abort();
    this.finish(this.onabort);
  }

  private finish(listener: Listener): void {
    this.finished = true;
    this.readyState = FetchBackedXhr.DONE;
    inFlight.delete(this);
    listener?.({} as ProgressEvent);
  }
}

/**
 * A fetch mock's answer as bytes. Real responses carry `arrayBuffer()`; test mocks often return a
 * Response-shaped object with only `text()` or `json()`.
 */
async function responseBytes(res: Response): Promise<ArrayBuffer> {
  if (typeof res.arrayBuffer === "function") return res.arrayBuffer();
  const text =
    typeof res.text === "function"
      ? await res.text()
      : typeof res.json === "function"
        ? JSON.stringify(await res.json())
        : "";
  return new TextEncoder().encode(text).buffer as ArrayBuffer;
}

function bodySize(body: unknown): number {
  if (body instanceof FormData) {
    let size = 0;
    for (const [, value] of body.entries()) {
      size += typeof value === "string" ? new Blob([value]).size : value.size;
    }
    return size;
  }
  if (body instanceof Blob) return body.size;
  return typeof body === "string" ? new Blob([body]).size : 0;
}
