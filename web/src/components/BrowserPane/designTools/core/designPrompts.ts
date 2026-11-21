// Chat messages for the Browser tab's design tools that lib/designModePrompt doesn't build
// (Annotate's is its design-mode message): Draw's, and the page address both name.

/**
 * The page's address as the agent is told it: its origin only. The path, query and fragment
 * can carry credentials no pattern tells from a route, and the screenshot shows the page.
 */
export function pageAddress(url: string): string {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return "(unknown page)";
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    return `${parsed.protocol} page`;
  }
  return parsed.origin;
}

/** The Draw message: the user's note, and what the attached screenshot shows. */
export function buildDrawPrompt(note: string, url: string): string {
  const request = note.trim() || "Make the changes I marked on the attached screenshot.";
  return (
    `${request}\n\n---\n[Design Mode — marked-up screenshot of the browser preview]\n` +
    `Page: ${pageAddress(url)}\nThe attached screenshot shows the page with my marks drawn on it.\n---`
  );
}
