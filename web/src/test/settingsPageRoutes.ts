// The server, as the real Settings page sees it in tests: an explicit table of requests and
// their answers. A request the table doesn't list is recorded, so a test can fail on it,
// rather than thrown, which React Query and the page would swallow.

export interface SettingsRoute {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  /** Path and query exactly as the page requests them, such as `/v1/hosts`. */
  url: string;
  body: unknown;
  status?: number;
  /** Answers only once this settles, so a test can see the page while the request is in flight. */
  hold?: Promise<unknown>;
}

/** What the page's requests go through, in place of the browser's fetch. */
export type SettingsFetch = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

export interface SettingsRouteTable {
  fetch: SettingsFetch;
  /** `METHOD /path?query` of every request no route answered, in order. */
  unmatched: string[];
  /** Answers `url` differently from now on, as when a scenario changes the server. */
  set: (route: SettingsRoute) => void;
  /** `METHOD /path?query` of each request asked and not yet answered, once per request. */
  inFlight: () => readonly string[];
  /** `METHOD /path?query` of every request a route answered, in the order they were answered. */
  answered: () => readonly string[];
}

export function settingsRouteTable(routes: readonly SettingsRoute[]): SettingsRouteTable {
  const table = new Map<string, SettingsRoute>();
  const keyOf = (method: string, url: string) => `${method} ${url}`;
  const set = (route: SettingsRoute) => table.set(keyOf(route.method ?? "GET", route.url), route);
  for (const route of routes) set(route);
  const unmatched: string[] = [];
  const asked: string[] = [];
  const answered: string[] = [];
  const answer: SettingsFetch = async (input, init) => {
    // A Request carries its own method and body; a string or URL is relative to the page.
    const request =
      input instanceof Request
        ? new Request(input, init)
        : new Request(new URL(String(input), window.location.origin), init);
    const url = new URL(request.url);
    const key = keyOf(request.method, `${url.pathname}${url.search}`);
    const route = table.get(key);
    if (!route) {
      unmatched.push(key);
      return new Response(JSON.stringify({ detail: "No test route" }), {
        status: 404,
        headers: { "content-type": "application/json" },
      });
    }
    asked.push(key);
    try {
      if (route.hold) await route.hold;
    } finally {
      asked.splice(asked.indexOf(key), 1);
    }
    answered.push(key);
    return new Response(JSON.stringify(route.body), {
      status: route.status ?? 200,
      headers: { "content-type": "application/json" },
    });
  };
  return {
    fetch: answer,
    unmatched,
    set,
    inFlight: () => [...asked],
    answered: () => [...answered],
  };
}
