import { useEffect, useState } from "react";

/** Blob URL for `blob`, created and revoked inside one effect so the URL a
 *  committed <img> points at is never revoked early (StrictMode double mount)
 *  and never leaks. Guarded: jsdom (tests) lacks createObjectURL. */
export function useObjectUrl(blob: Blob | null): string | undefined {
  const [url, setUrl] = useState<string>();
  useEffect(() => {
    if (!blob || typeof URL.createObjectURL !== "function") {
      setUrl(undefined);
      return;
    }
    const objectUrl = URL.createObjectURL(blob);
    setUrl(objectUrl);
    return () => URL.revokeObjectURL(objectUrl);
  }, [blob]);
  return url;
}
