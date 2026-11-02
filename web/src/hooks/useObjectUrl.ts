import { useEffect, useState } from "react";

/** Blob URL for `blob`, created and revoked inside one effect so the URL a
 *  committed <img> points at is never revoked early (StrictMode double mount)
 *  and never leaks. Guarded: jsdom (tests) lacks createObjectURL. */
export function useObjectUrl(blob: Blob | null): string | undefined {
  const [entry, setEntry] = useState<{ blob: Blob; url: string }>();
  useEffect(() => {
    if (!blob || typeof URL.createObjectURL !== "function") {
      setEntry(undefined);
      return;
    }
    const url = URL.createObjectURL(blob);
    setEntry({ blob, url });
    return () => URL.revokeObjectURL(url);
  }, [blob]);
  // The render after `blob` changes must not hand out the URL being revoked.
  return entry?.blob === blob ? entry.url : undefined;
}
