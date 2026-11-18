// What a browser tab is called: the page's own title, else its host, else a
// fallback such as "Browser 2". Chromium reports an untitled page's URL as its
// title, which isn't a title.

export interface BrowserPageFacts {
  url?: string | null;
  title?: string | null;
}

export function browserPageLabel(page: BrowserPageFacts | undefined, fallback: string): string {
  const title = page?.title?.trim();
  if (page?.url) {
    try {
      const hostname = new URL(page.url).hostname;
      const urlTitle = page.url.replace(/^https?:\/\//, "");
      if (
        title &&
        title !== page.url &&
        title !== urlTitle &&
        title !== urlTitle.replace(/\/$/, "")
      ) {
        return title;
      }
      if (hostname) return hostname;
    } catch {
      // Blank or invalid URLs keep the fallback.
    }
  }
  return title && title !== "about:blank" ? title : fallback;
}
