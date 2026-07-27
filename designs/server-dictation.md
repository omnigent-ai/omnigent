# Server-side streaming dictation

## Problem

The composer mic button (`web/src/components/ComposerMicButton.tsx`) relies on
the browser Web Speech API. That API is only backed by a real recognizer in
official Chrome/Safari builds (Google/Apple cloud speech); it is unavailable
in Electron, Firefox, Chromium, and most self-hosted contexts. Today the
button renders nothing (or "Dictation unavailable") in those environments —
`web/electron/README.md` documents the gap and prescribes the fix: capture
audio in the client and transcribe it on the Omnigent server.

This design adds that path: a streaming speech-to-text WebSocket on the
server, backed by a local [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)
model (CPU, no cloud, no per-request cost), with the mic button falling back
to it whenever Web Speech is unavailable.

## Goals

- Dictation works in Electron, Firefox/Chromium, and the iOS/Android wrappers
  (mic permissions are already wired in all three).
- Audio never leaves the operator's infrastructure.
- Live partial transcripts stream into the composer while the user speaks
  (the Web Speech path today only inserts final utterances).
- Zero new required dependencies: the STT engine ships as an optional extra
  (`omnigent[dictation]`), imported lazily, mirroring the `s3`/`modal`/
  `daytona` extras' posture. Servers without the extra (or without models)
  report `available: false` and the web UI silently keeps its current
  behavior.

## Non-goals

- Voice *conversations* (TTS replies, wake words, hands-free turn taking).
- Replacing the Web Speech path where it works today.
- Terminal REPL dictation (possible follow-up; shares the engine).
- Speaker diarization, translation, non-English models beyond whatever
  sherpa-onnx model the operator installs.

## Server

### Engine — `omnigent/server/dictation.py`

A small engine layer isolates the recognizer behind a protocol so tests
(and alternate backends, e.g. Whisper or an OpenAI-compatible
transcription API) don't need the native dependency:

```python
class DictationStreamHandle(Protocol):
    def feed_pcm16(self, data: bytes) -> DictationUpdate: ...  # decode a chunk
    def finish(self) -> str: ...                              # flush tail, final text
    def close(self) -> None: ...                              # release (client vanished)

@dataclass(frozen=True)
class DictationUpdate:
    partial: str          # current in-progress utterance, display-ready (revisable)
    finalized: str | None # utterance completed by endpointing, if any
```

Emitted text is **display-ready** — an engine that needs punctuation/casing
applies it internally before returning, so the route and protocol stay
engine-agnostic. Most modern models (Whisper, Parakeet) punctuate
themselves; sherpa is the exception (see below).

**Engine registry.** Engines are registered by name and selected via
`OMNIGENT_DICTATION_ENGINE`:

```python
register_engine("sherpa", lambda: SherpaDictationEngine(...), available=_sherpa_available)
register_engine("fake", FakeDictationEngine)
```

Adding an engine (Whisper, Parakeet, a hosted API) is one `register_engine`
call with a factory and an optional availability probe — no edits to
`get_engine` or `engine_availability`. Third-party engines register
themselves on import. The default (unset env var) is `sherpa`.

`SherpaDictationEngine` implements the protocol with a process-wide
`OnlineRecognizer` (streaming transducer: `encoder/decoder/joiner + tokens`)
shared across connections — the ~650 MB weights load once — plus one
recognizer *stream* per WebSocket. Endpointing folds completed utterances
into `finalized` and resets the stream, exactly the loop proven in pi-voice.
An optional `OnlinePunctuation` model re-punctuates partials/finals
(lowercase + strip punctuation before re-adding, throttled) so the live
preview reads like a sentence. This punctuation is **internal** to the
sherpa engine — the raw transducer emits lowercase, unpunctuated text, so
the streams beautify before returning; it is not part of the protocol.

Decode calls are CPU-bound → they run via `asyncio.to_thread`, serialized by
a per-engine `threading.Lock` (sherpa recognizer streams are not documented
thread-safe), with a module-level semaphore capping concurrent dictation
connections (default 2, `OMNIGENT_DICTATION_MAX_STREAMS`).

### Configuration

| Env var | Default | Meaning |
|---|---|---|
| `OMNIGENT_DICTATION_MODEL_DIR` | `~/.omnigent/models/dictation/asr` | dir containing `encoder*.onnx`, `decoder*.onnx`, `joiner*.onnx`, `tokens.txt` |
| `OMNIGENT_DICTATION_PUNCT_DIR` | `~/.omnigent/models/dictation/punct` | optional online-punctuation model dir (`model*.onnx` + `bpe.vocab`) |
| `OMNIGENT_DICTATION_MAX_STREAMS` | `2` | concurrent dictation WebSockets |
| `OMNIGENT_DICTATION_ENGINE` | unset (`sherpa`) | engine to use by registered name (`sherpa`, `remote`, `fake`) |
| `OMNIGENT_DICTATION_REMOTE_URL` | unset | worker stream URL for the `remote` engine, e.g. `ws://venus:8100/v1/dictation/stream` |

`scripts/fetch-dictation-models.sh` downloads a known-good pair (streaming
Nemotron 0.6 B int8 + English online punctuation, both Apache-2.0 upstream)
into the default locations. Availability is computed lazily and cached:
extra installed **and** ASR model dir populated.

**Hardware sizing.** Any sherpa-onnx streaming transducer directory works —
point `OMNIGENT_DICTATION_MODEL_DIR` at it. Streaming dictation needs ≥1×
realtime decode; measured with this engine loop (int8, 4 threads, 100 ms
chunks):

| Model | Apple M-series | Intel N95 (4 E-cores, loaded box) | RAM |
|---|---|---|---|
| Nemotron 0.6 B (fetch-script default) | ~9× realtime | 0.6–0.7× — **too slow** | ~1.0 GB |
| `streaming-zipformer-en-2023-06-26` | — | 1.4–2.3× realtime | ~190 MB |
| `streaming-zipformer-en-20M` | — | 3.6–4.9× realtime | ~130 MB |

On N100/N95-class mini-PC servers, use the mid-size zipformer (accuracy held
up in spot checks; the 20 M model audibly degrades) and consider
`OMNIGENT_DICTATION_MAX_STREAMS=1`.

**Other languages.** The engine is language-agnostic — dictation speaks
whatever language the installed model was trained on. The
[sherpa-onnx streaming-model catalog](https://k2-fsa.github.io/sherpa/onnx/pretrained_models/online-transducer/index.html)
includes Chinese, Chinese/English bilingual
(`sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20`), French
(`sherpa-onnx-streaming-zipformer-fr-2023-04-14`), Korean, and more; point
`OMNIGENT_DICTATION_MODEL_DIR` at any of them. Two caveats: the fetch
script's punctuation model is English-only, so leave
`OMNIGENT_DICTATION_PUNCT_DIR` unpopulated for other languages (raw
recognizer output is emitted as-is), and the mic button's `lang` prop only
affects the Web Speech path — the server path's language is decided by the
operator's model choice.

### Remote worker

Where a mini-PC server can't run the model an operator wants at realtime, the
`remote` engine relays each take to a **dictation worker** on a beefier LAN
box. The worker is just `create_dictation_router` served on its own — it
speaks the exact same wire protocol the browser does (PCM frames up,
transcript events down), so no new protocol or code path was needed. The
browser never talks to the worker; the main server authenticates the user on
its own route, then relays over a `websockets` client.

Run the worker wherever the models live (it is **unauthenticated** — bind it
to a trusted LAN/VPN only):

```
pip install omnigent[dictation] && scripts/fetch-dictation-models.sh
python -m omnigent.server.dictation_worker --host 0.0.0.0 --port 8100
```

Then select the `remote` engine on the main server via env vars — no CLI
integration is required:

```
OMNIGENT_DICTATION_ENGINE=remote \
OMNIGENT_DICTATION_REMOTE_URL=ws://<worker-host>:8100/v1/dictation/stream \
omnigent server ...
```

`RemoteDictationEngine` registers by name like every other engine (no changes
to the route, protocol, or selection logic). `_RemoteStream` bridges the
worker's async push events into the synchronous handle interface via a daemon
reader thread, and `close()` releases the worker's capacity slot promptly.
Fallback is per take: if the worker is unreachable and local models are
installed, a lazily-built local sherpa engine serves the take instead (its
weights cost no RAM until the worker actually goes down); each new take
retries the worker first.

Client timeouts (`web/src/lib/dictation.ts`) are widened to exceed the
worker's cold-load budget (`_REMOTE_READY_TIMEOUT_S` / `_REMOTE_STOP_TIMEOUT_S`
in `dictation.py`) so a relayed take doesn't time out on the browser side just
as the worker finishes loading its model.

### Routes — `omnigent/server/routes/dictation.py`

`create_dictation_router(*, auth_provider=None, engine_provider=None)`,
registered in `create_app` under `/v1` like every other router. Dictation is
not session-scoped (the new-chat composer has no session yet), so auth is
identity-level only: authenticated user required when an auth provider is
configured, open in single-user/dev mode — the same posture as
`GET /v1/harnesses`.

Availability rides the existing boot-time capability probe —
`dictation_available` on **`GET /v1/info`** — rather than a dedicated
endpoint; the UI needs one boolean, once per page load.

- **`WS /v1/dictation/stream`** — wire protocol (documented in the module
  docstring, mirroring `terminal_attach.py`):
  - **Client → server, binary frames**: raw 16 kHz mono s16le PCM.
  - **Client → server, text frames**: JSON control messages.
    `{"type": "stop"}` requests a flush; unknown shapes are ignored for
    forward compatibility.
  - **Server → client, text frames**: JSON events.
    - `{"type": "ready"}` — sent once after accept; the client may start
      streaming audio.
    - `{"type": "partial", "text": ...}` — revisable in-progress utterance,
      throttled to ~6 Hz.
    - `{"type": "final", "text": ...}` — an utterance completed by
      endpointing; the client appends it and clears the partial region.
    - `{"type": "stopped", "text": ...}` — response to `stop`: the flushed
      tail utterance (possibly empty). The server closes after sending it.
    - `{"type": "error", "message": ...}` — fatal; server closes.

The route holds no session state; a connection is one dictation take.

## Web

### Capture — `web/src/lib/dictation.ts`

`DictationSession` owns the full client pipeline:
`getUserMedia({audio})` → `AudioContext` → `AudioWorkletNode` (the worklet,
inlined as a Blob module, downsamples from the context rate to 16 kHz and
converts Float32 → Int16, posting 100 ms chunks) → binary WS frames via
`resolveWebSocketUrl("/v1/dictation/stream")` (the same host seam the
terminal-attach and session-updates sockets ride, so embed hosts and the
Vite dev proxy keep working). Callbacks: `onPartial`, `onFinal`, `onError`;
`stop()` sends `{"type":"stop"}`, resolves with the flushed tail, and
releases the mic tracks and audio context.

Availability comes from the existing `/v1/info` capability context
(`useServerInfo().dictation_available`) — no extra request.

### Mic button — `ComposerMicButton.tsx`

Mode selection: **Web Speech when the browser has a working one, server
dictation otherwise** — no behavior change for Chrome/Safari users;
Electron, Firefox, and Chromium gain a working button. "Working" cannot be
detected statically: Electron and plain Chromium expose the
`SpeechRecognition` constructor but its cloud backend rejects them at
runtime with a `network` error. So Web Speech stays primary whenever the
constructor exists, and a take that dies with `network` falls back to the
server **for that take** (retried immediately, so the user's click still
lands); the next take tries Web Speech again, so a transient blip in real
Chrome never permanently downgrades the page. With no constructor at all
(Firefox), takes go to the server directly.

New optional prop `onInterim?: (text: string) => void`. In server mode the
button emits `onInterim` for partial frames and the existing
`onTranscript` for finals. Both composers (`ChatPage`, `NewChatDialog`)
share a small hook, `useDictationInsert(setValue)`, that appends finals and
maintains a replaceable trailing interim region in the textarea value, so
text forms live while speaking. When `onInterim` is absent (Web Speech
mode), behavior is exactly today's.

## Testing

- **Server (pytest, `tests/server/routes/test_dictation.py`)**: drive the
  real route with `TestClient.websocket_connect` and a fake engine injected
  through `engine_provider` — no sherpa dependency in CI. Cases:
  `/v1/info` availability (with and without an engine), ready→partial→final
  →stopped flow, stop-flush, auth rejection with a no-identity provider,
  stream-cap rejection.
- **Engine unit tests** skip unless sherpa-onnx and models are present
  (developer machines), keeping CI hermetic.
- **Web (Vitest, `ComposerMicButton.test.tsx` + `dictation.test.ts`)**:
  mode selection, partial/final callback flow against a mocked WebSocket and
  mocked AudioWorklet capture.
- **e2e (Playwright, `tests/e2e_ui/`)**: a fake engine selected via env
  (`OMNIGENT_DICTATION_ENGINE=fake`, emits a scripted transcript) lets the
  full browser→WS→server→composer loop run headless without a mic:
  the test grants fake mic permissions, clicks the mic button, and asserts
  the scripted text lands in the composer.

## Rollout / compatibility

- No schema changes, no migrations, no new required deps.
- Servers without the extra: `/v1/info` reports `dictation_available: false`;
  the web UI behaves exactly as today.
- Old web clients against new servers: unaffected (new route + one new
  `/v1/info` field only).
- New web clients against old servers: `/v1/info` lacks the field → treated
  as unavailable → today's behavior.
