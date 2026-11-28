// Whether this page is going away. A page that is unloading aborts its open requests, and their
// failure handlers still run as it goes; those handlers shouldn't undo work the server may have
// finished, or save state the next page would read.

let pageHiding = false;
if (typeof window !== "undefined") {
  window.addEventListener("pagehide", () => {
    pageHiding = true;
  });
  // A page restored from the back/forward cache is live again.
  window.addEventListener("pageshow", () => {
    pageHiding = false;
  });
}

/** Whether the page has been hidden for unloading and not shown again. */
export function isPageHiding(): boolean {
  return pageHiding;
}
