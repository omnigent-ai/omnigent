// jsdom's XMLHttpRequest would reach the network. This one sends through the global `fetch`, so
// a test's fetch mock answers uploads too. The whole body counts as sent once the fetch answers,
// unless a test reports progress itself with `progress()`.

type Listener = ((event: ProgressEvent) => void) | null;

/** Every request sent through the double, newest last, for tests that drive progress by hand. */
export const sentXhrs: FetchBackedXhr[] = [];

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
    this.upload.onprogress?.({ loaded, total, lengthComputable: true } as ProgressEvent);
  }

  send(body: unknown = null): void {
    this.body = body;
    sentXhrs.push(this);
    const size = bodySize(body);
    void (async () => {
      try {
        const res = await globalThis.fetch(this.url, {
          method: this.method,
          headers: Object.fromEntries(this.requestHeaders.entries()),
          body: body as BodyInit | null,
          credentials: this.withCredentials ? "include" : "same-origin",
          signal: this.controller.signal,
        });
        if (this.finished) return;
        // Test fetch mocks often return a Response-shaped object with only `text()` or `json()`.
        const bytes =
          typeof res.arrayBuffer === "function"
            ? await res.arrayBuffer()
            : new TextEncoder().encode(typeof res.text === "function" ? await res.text() : "")
                .buffer;
        if (this.finished) return;
        this.progress(size, size);
        this.status = res.status;
        this.statusText = res.statusText;
        this.responseHeaders = new Headers(res.headers);
        this.response =
          this.responseType === "arraybuffer" ? bytes : new TextDecoder().decode(bytes);
        this.finish(this.onload);
      } catch {
        if (!this.finished) this.finish(this.onerror);
      }
    })();
  }

  abort(): void {
    if (this.finished) return;
    this.controller.abort();
    this.finish(this.onabort);
  }

  private finish(listener: Listener): void {
    this.finished = true;
    this.readyState = FetchBackedXhr.DONE;
    listener?.({} as ProgressEvent);
  }
}

function bodySize(body: unknown): number {
  if (body instanceof FormData) {
    let size = 0;
    for (const [, value] of body.entries()) {
      size += typeof value === "string" ? value.length : value.size;
    }
    return size;
  }
  if (body instanceof Blob) return body.size;
  return typeof body === "string" ? body.length : 0;
}
