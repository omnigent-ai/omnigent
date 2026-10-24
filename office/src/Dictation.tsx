// Microphone dictation through Omnigent's server-side speech engine, proxied
// by the office service. Audio is transcribed by the connected Omnigent
// server, not a cloud browser service. Transcripts land in an editable field
// and are never sent automatically.

import { useEffect, useRef, useState } from "react";

type DictationStatus = "idle" | "connecting" | "listening" | "stopping" | "error";

interface DictationButtonProps {
  available: boolean;
  /** Called with each finished utterance; the caller appends it to its editable draft. */
  onText: (text: string) => void;
}

export function DictationButton({ available, onText }: DictationButtonProps) {
  const [status, setStatus] = useState<DictationStatus>("idle");
  const [partial, setPartial] = useState("");
  const [error, setError] = useState<string | null>(null);
  const cleanupRef = useRef<(() => void) | null>(null);
  const socketRef = useRef<WebSocket | null>(null);
  const onTextRef = useRef(onText);
  onTextRef.current = onText;

  useEffect(() => () => cleanupRef.current?.(), []);

  if (!available) return null;

  const start = async () => {
    setError(null);
    setStatus("connecting");
    try {
      const media = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true } });
      const audio = new AudioContext();
      await audio.audioWorklet.addModule("/dictation-worklet.js");
      const source = audio.createMediaStreamSource(media);
      const downsampler = new AudioWorkletNode(audio, "dictation-downsampler");
      const scheme = window.location.protocol === "https:" ? "wss" : "ws";
      const socket = new WebSocket(`${scheme}://${window.location.host}/office/api/dictation/stream`);
      socket.binaryType = "arraybuffer";
      socketRef.current = socket;
      const cleanup = () => {
        downsampler.port.onmessage = null;
        source.disconnect();
        downsampler.disconnect();
        media.getTracks().forEach((track) => track.stop());
        void audio.close().catch(() => undefined);
        if (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING) socket.close();
        socketRef.current = null;
        cleanupRef.current = null;
      };
      cleanupRef.current = cleanup;
      socket.onmessage = (message) => {
        const event = JSON.parse(String(message.data)) as { type: string; text?: string; message?: string };
        if (event.type === "ready") {
          setStatus("listening");
          source.connect(downsampler);
          downsampler.port.onmessage = (chunk) => {
            if (socket.readyState === WebSocket.OPEN) socket.send(chunk.data as ArrayBuffer);
          };
        } else if (event.type === "partial") {
          setPartial(event.text ?? "");
        } else if (event.type === "final" || event.type === "stopped") {
          setPartial("");
          if (event.text?.trim()) onTextRef.current(event.text.trim());
          if (event.type === "stopped") {
            cleanup();
            setStatus("idle");
          }
        } else if (event.type === "error") {
          setError(event.message ?? "Dictation failed.");
          cleanup();
          setStatus("error");
        }
      };
      socket.onclose = (closed) => {
        if (closed.code === 1013) setError("Dictation is busy; try again shortly.");
        cleanup();
        setStatus((current) => (current === "error" ? current : "idle"));
      };
    } catch (caught) {
      cleanupRef.current?.();
      setError((caught as Error).message);
      setStatus("error");
    }
  };

  const stop = () => {
    const socket = socketRef.current;
    if (socket?.readyState === WebSocket.OPEN) {
      setStatus("stopping");
      socket.send(JSON.stringify({ type: "stop" }));
    } else {
      cleanupRef.current?.();
      setStatus("idle");
    }
  };

  const active = status === "connecting" || status === "listening" || status === "stopping";
  return (
    <span className="dictation">
      <button type="button" aria-pressed={active} onClick={() => (active ? stop() : void start())} disabled={status === "stopping"}>
        {active ? "■ Stop dictation" : "🎤 Dictate"}
      </button>
      {status === "listening" && partial ? <span className="muted small" aria-live="polite"> {partial}</span> : null}
      {error ? (
        <span className="error-text small" role="alert">
          {" "}
          {error}
        </span>
      ) : null}
    </span>
  );
}
