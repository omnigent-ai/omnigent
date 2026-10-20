const VIDEO_EXTENSIONS = /\.(?:mp4|webm|mov|m4v|ogv)$/i;

export function formatVideoTime(seconds: number): string {
  const whole = Math.floor(seconds);
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}

/** Recognize recording files without mistaking TypeScript's video/mp2t MIME for video. */
export function isVideoFile(path: string, contentType?: string | null): boolean {
  const type = contentType?.split(";")[0].trim().toLowerCase();
  if (type && type !== "application/octet-stream") {
    return type.startsWith("video/") && type !== "video/mp2t";
  }
  return VIDEO_EXTENSIONS.test(path.split(/[?#]/)[0]);
}

/** Only direct HTTP media links may become remote players. */
export function isRemoteVideoUrl(href: string): boolean {
  if (!/^(?:https?:)?\/\//i.test(href)) return false;
  try {
    return isVideoFile(new URL(href.startsWith("//") ? `https:${href}` : href).pathname);
  } catch {
    return false;
  }
}
