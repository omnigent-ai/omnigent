export interface VideoChapter {
  time: number;
  end: number;
  title: string;
}

/** Use the browser's WebVTT parser without loading any video bytes. */
export function parseVideoChapters(content: string, signal: AbortSignal): Promise<VideoChapter[]> {
  const blob = new Blob([content], { type: "text/vtt" });
  if (signal.aborted || blob.size > 65_536) return Promise.resolve([]);
  return new Promise((resolve) => {
    const video = document.createElement("video");
    const track = document.createElement("track");
    const url = URL.createObjectURL(blob);
    const finish = (chapters: VideoChapter[]) => {
      clearTimeout(timeout);
      signal.removeEventListener("abort", cancel);
      track.onload = null;
      track.onerror = null;
      video.remove();
      URL.revokeObjectURL(url);
      resolve(chapters);
    };
    const cancel = () => finish([]);
    const timeout = setTimeout(cancel, 5_000);
    signal.addEventListener("abort", cancel, { once: true });
    video.hidden = true;
    track.kind = "chapters";
    track.onload = () => finish(readChapterCues(track.track.cues));
    track.onerror = cancel;
    video.append(track);
    document.body.append(video);
    track.src = url;
    track.track.mode = "hidden";
  });
}

export function readChapterCues(cues: TextTrackCueList | null): VideoChapter[] {
  if (!cues || cues.length > 200) return [];
  const chapters: VideoChapter[] = [];
  const keys = new Set<string>();
  for (const cue of Array.from(cues)) {
    if (!("text" in cue) || typeof cue.text !== "string") continue;
    const title = cue.text.trim();
    if (!title || title.length > 500) continue;
    const key = `${cue.startTime}:${cue.endTime}:${title}`;
    if (keys.has(key)) continue;
    keys.add(key);
    chapters.push({ time: cue.startTime, end: cue.endTime, title });
  }
  return chapters.sort((a, b) => a.time - b.time);
}
