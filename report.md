# Omnigent Voice Dictation Assessment

Assessment date: 2026-08-01

Repository state reviewed: commit `86d2ab87` (`refactor: narrow session helper boundaries (#3851)`). The checkout contains the Omnigent 0.7.0 changelog dated 2026-07-27, including the dictation release items.

## Phase 1 branch update

The baseline assessment below describes the repository before this branch. `feat/dictation-phase-one` now implements the Phase 1 hardening work:

- Device-local path, browser language, and microphone preferences with explicit privacy guidance.
- Authenticated remote workers, verified TLS/private CA support, health/readiness and warmup, sanitized diagnostics, resource limits, and OpenTelemetry metrics.
- Focused-window shortcut wording instead of implying an OS-global shortcut.
- Caret- and selection-aware insertion with safe interim ownership and Escape restoration.
- One microphone capture in server mode, reused by the input meter with abortable startup.
- Visible and announced starting, listening, stopping, completion, and actionable error states.

Apple MLX, Whisper, NVIDIA engine adapters, and repository-aware token resolution remain later phases.

## Executive summary

Omnigent already implements the main server-backed voice input flow described in the question:

1. The user clicks the microphone in the chat or new-chat composer.
2. The browser captures microphone audio.
3. It converts the audio to 16 kHz mono PCM16 and streams 100 ms chunks over a WebSocket to the Omnigent server.
4. The server returns revisable partial text and finalized text.
5. The web client inserts that text into the composer draft. It does not automatically send the message.

This works in Electron, Firefox, and Chromium when server dictation is installed and configured. It can also work when the web client and Omnigent server are on different machines. An optional relay can send the audio onward from the main Omnigent server to a dedicated transcription worker.

The shipped local engine is currently only a CPU-backed sherpa-onnx streaming transducer. The code does not currently implement Apple MLX, mlx-whisper, parakeet-mlx, faster-whisper, whisper.cpp, NVIDIA NeMo, Riva, or Speech NIM engines. The engine registry makes these integrations feasible, but they still require adapters, packaging, configuration, and tests.

This Apple M5 machine is technically a strong target for an MLX engine, but the current checkout is not ready to transcribe locally as configured:

- Architecture: `arm64`
- CPU: Apple M5
- `sherpa_onnx`: not installed in the checked environment
- `~/.omnigent/models/dictation`: absent
- The existing `.venv` is already based on Python 3.12

That is a setup state, not a hardware limitation. The existing sherpa path can run locally after installing the extra and models. An MLX path would require new code.

The requested repository-aware correction of spoken file and function names does not exist today. Dictation is plain speech-to-text. The best implementation is a two-stage pipeline:

```text
audio -> acoustic transcription -> repository-aware token resolver -> composer
```

Use acoustic hotwords when an engine supports them, but retain the resolver for exact spelling, ambiguity handling, and compatibility with browser Web Speech.

## Direct answers

### Can Omnigent dictate into the composer today?

Yes. Both the active chat composer and new-chat composer have a microphone button. Server partials appear live, finalized utterances replace their partial region, and the result remains an editable draft.

Key code:

- `web/src/pages/ChatPage.tsx:5086-5123`
- `web/src/shell/NewChatDialog.tsx:3369-3394`
- `web/src/components/ComposerMicButton.tsx:98-229,331-424,426-512`
- `web/src/hooks/useDictationInsert.ts:42-68`

### Can a browser send speech over the network to an Omnigent server?

Yes. This exact flow is implemented. The browser opens `WS /v1/dictation/stream`, sends binary PCM frames, and receives `partial`, `final`, and `stopped` text events.

```text
remote browser microphone
  -> ws(s)://omnigent-server/v1/dictation/stream
  -> configured recognition engine
  -> transcript events
  -> browser composer draft
```

The WebSocket URL uses the same host-resolution seam as other Omnigent sockets, so remote and embedded server hosts are supported (`web/src/lib/dictation.ts:12-15,221-270`).

### Can recognition be offloaded to another box?

Yes. Configure the main server with the `remote` engine. The browser connects only to the main server, which relays audio to the worker. The main route requires identity when an auth provider is configured, but remains open in single-user/development mode:

```text
browser -> main Omnigent server -> dictation worker -> main server -> browser
```

The worker serves the same PCM/transcript protocol (`omnigent/server/routes/dictation.py`, `omnigent/server/dictation_worker.py`). On this branch it requires a shared bearer token and supports verified TLS; non-loopback plaintext requires an explicit insecure override.

### Is transcription local or cloud-based?

It depends on the client and selected path:

| Situation | Actual path |
|---|---|
| Electron with server dictation available | Goes directly to the Omnigent server |
| Firefox without Web Speech | Goes directly to the Omnigent server |
| Plain Chromium with a broken Web Speech backend | Tries Web Speech, then falls back to the server on `network` error |
| Official Chrome/Safari with Web Speech | Prefers browser Web Speech, which may use Google/Apple infrastructure |
| Main server using `sherpa` | Recognition runs in the Omnigent server process on CPU |
| Main server using `remote` | Audio is relayed to the configured worker |

Therefore, "audio never leaves your server" applies to the server recognition path and operator-controlled worker infrastructure. It is not a guarantee for Chrome/Safari while Web Speech remains preferred. There is no current user setting to force server-only recognition in those browsers.

### Is the Command-Option-V shortcut system-wide?

No. `Cmd+Alt+V` on macOS and `Ctrl+Alt+V` elsewhere is a browser-window key listener (`web/src/hooks/useVoiceDictationHotkey.ts:17-63`). It works from most focused surfaces in the app, excluding xterm and Monaco. There is no Electron `globalShortcut` registration, so it cannot start dictation while another application has focus.

### Does it recognize repository filenames and symbols specially?

No. The transcript is appended as ordinary text. There is no repository vocabulary, symbol index, contextual bias list, phonetic resolver, or ambiguity UI. Dictation also appends at the end of the draft rather than inserting at the caret or replacing a selection (`web/src/hooks/useDictationInsert.ts:25-64`).

## Current feature inventory

### Client capture and composer UX

- Microphone controls in active-chat and new-chat composers.
- Web Speech support through `SpeechRecognition` and `webkitSpeechRecognition`.
- Automatic server fallback on a Web Speech `network` failure.
- Direct server selection in Electron when server dictation is available.
- `getUserMedia` with mono, echo cancellation, and noise suppression.
- Inline AudioWorklet resampling and Float32-to-PCM16 conversion.
- 100 ms binary audio frames.
- Live, revisable partial text for the server path.
- Finalized text after endpoint detection or stop flush.
- Safe partial replacement that avoids deleting subsequent user edits.
- `Enter` stops dictation and keeps text. It does not submit the message.
- `Escape` cancels dictation and restores the pre-dictation draft.
- Animated input-level bars while listening.
- Busy, permission-denied, and unavailable states.
- Capability gating through `GET /v1/info` and `dictation_available`.
- Microphone permission plumbing in Electron, iOS, and Android wrappers.

The Web Speech path disables interim results, so only server dictation currently provides live partials (`web/src/components/ComposerMicButton.tsx:153-157`).

### Server protocol

The protocol is defined in `omnigent/server/routes/dictation.py:12-32`.

Client to server:

```text
binary: raw 16,000 Hz, mono, signed 16-bit little-endian PCM
text:   {"type":"stop"}
```

Server to client:

```json
{"type":"ready"}
{"type":"partial","text":"..."}
{"type":"final","text":"..."}
{"type":"stopped","text":"..."}
{"type":"error","message":"..."}
```

Partials are change-detected and limited to one push every 150 ms. One WebSocket represents one take. There is no audio upload route, compressed audio negotiation, transcript persistence, or conversation state in this endpoint.

### Shipped engines

| Engine | Purpose | Shipped runtime engine |
|---|---|---|
| `sherpa` | Local sherpa-onnx online transducer | Yes |
| `remote` | Relay to another compatible dictation worker | Yes, trusted networks only until transport is hardened |
| `fake` | Deterministic tests | No |

The registry and engine contract are in `omnigent/server/dictation.py:129-219`. A stream implements:

```python
feed_pcm16(data: bytes) -> DictationUpdate
finish() -> str
close() -> None
```

This is a useful extension seam, but it only carries text. It has no request options for language, hotwords, prompt, model, or repository context, and no event fields for timestamps, confidence, or detected language.

### Current sherpa behavior

- Process-wide recognizer, per-connection stream.
- Streaming transducer model made of encoder, decoder, joiner, and token files.
- Hardcoded `cpu` provider and four ASR threads.
- Greedy decoding.
- Endpoint detection after configured silence or a 30-second utterance.
- Optional CPU punctuation and casing model.
- One second of synthetic silence to flush trailing speech on stop.
- Generic support for compatible sherpa online transducer model directories.
- Default fetch script installs an English Nemotron streaming model and English punctuation model.

The provider is hardcoded at `omnigent/server/dictation.py:354-380`. No CUDA, Metal, Core ML, TensorRT, or MLX provider can be selected through configuration.

### Availability and concurrency

- The default engine is `sherpa`.
- Model loading is lazy on the first take.
- A successful process-wide engine remains cached until restart.
- Default capacity is two admitted WebSockets per server process.
- Actual sherpa recognizer operations are serialized behind one lock.
- Excess connections close with WebSocket code 1013.
- Capacity is process-local, not coordinated across multiple Uvicorn workers.

### Remote worker behavior

- The main route requires authentication when an auth provider is configured; it is open in single-user/development mode.
- The main server relays raw PCM and receives transcript events.
- Worker cold-start allowance is 30 seconds.
- A worker connection has a 5-second open timeout and 10-second stop timeout.
- If worker stream creation fails, local sherpa is used for that take when available.
- If the worker dies after a take starts, that take fails. The next take retries the worker.
- `/v1/info` considers `remote` available when a URL is configured; it does not health-check the worker.

### Existing test coverage

- Engine availability, registry, fake streaming, and optional real-sherpa smoke test.
- WebSocket ready/partial/final/stop, authentication, cleanup, and capacity.
- Real loopback remote-worker relay using the fake engine.
- Frontend mode selection, fallback, errors, hotkey, commit, discard, and partial insertion.
- Chromium end-to-end microphone-to-composer flow using fake audio and fake recognition.

Relevant suites:

- `tests/server/test_dictation_engine.py`
- `tests/server/routes/test_dictation.py`
- `tests/server/test_dictation_remote.py`
- `web/src/components/ComposerMicButton.test.tsx`
- `web/src/hooks/useDictationInsert.test.tsx`
- `web/src/hooks/useVoiceDictationHotkey.test.tsx`
- `tests/e2e_ui/chat/test_dictation.py`

## What is not implemented

- MLX or Metal-backed recognition.
- Whisper, faster-whisper, or whisper.cpp engine adapters.
- NVIDIA CUDA, NeMo, Parakeet, Riva, or Speech NIM adapters.
- Per-take model, language, translation, prompt, or vocabulary selection.
- Request-time hotword or phrase boosting.
- Word timestamps, confidence, or detected-language events.
- Repository filename, symbol, command, or technical-vocabulary correction.
- Repository-aware correction after insertion.
- OS-global Electron shortcut.
- Terminal REPL dictation.
- Voice commands, wake words, hands-free turn submission, TTS responses, or voice conversations.
- Audio-file transcription endpoint.
- Speaker diarization.
- Persistent transcript or audio history.
- Model manager or server-model language picker. The browser language preference does not change a fixed server model.
- Account-synchronized dictation preferences. Phase 1 preferences are intentionally device-local.
- Native Electron, iOS, or Android ASR. These wrappers use the shared web path.

## Engine integration assessment

### Recommended compatibility matrix

| Backend | Best deployment | Real streaming | Context bias | Fit with current contract | Recommendation |
|---|---|---:|---|---|---|
| Existing sherpa-onnx | Portable CPU server | Yes | Possible upstream, not wired here | Excellent | Keep as portable default |
| `parakeet-mlx` | Apple Silicon server/Electron companion | Yes, draft and finalized tokens | Chosen-word enhancement is not currently a mature MLX feature | Very good | First Apple-specific engine |
| `mlx-whisper` | Apple Silicon, multilingual/translation use | No native online decoder | `initial_prompt`; no deterministic repository correction | Moderate | Add only for Whisper-specific needs |
| `faster-whisper` | CPU or NVIDIA CUDA worker | Requires rolling/local-agreement policy | `hotwords` and `initial_prompt` | Good after streaming layer | First general Whisper engine |
| `whisper.cpp` | Native desktop/mobile or portable binary | Sliding-window real-time example | Prompt and grammar facilities | Moderate from Python | Strong later native option |
| NVIDIA NeMo streaming | Dedicated Linux NVIDIA worker | Yes | Strong phrase boosting for CTC/RNNT/TDT | Good behind worker | Best open NVIDIA path |
| NVIDIA Speech NIM/Riva | Managed NVIDIA GPU deployment | Yes | Production word boosting and metadata | Protocol adapter needed | Enterprise/scale option |
| Whisper-Flow project | Reference rolling-window service | Yes by repeated Whisper inference | Limited | Duplicates server stack | Borrow policy, do not embed dependency |

### Apple Silicon recommendation

Implement `parakeet-mlx` first for this device.

Reasons:

- It is designed for Apple Silicon and MLX.
- Its `transcribe_stream()` API accepts incremental audio.
- Its draft and stable tokens can drive Omnigent partial updates; the adapter must still endpoint and combine them into Omnigent's utterance-level `finalized` event.
- It provides token/word alignment data that can support stable partial commitment later.
- It avoids building a rolling-window streaming algorithm around an offline Whisper API.

Suggested packaging:

```toml
dictation-mlx = ["parakeet-mlx>=<validated-version>,<next-breaking-version>"]
```

Suggested selector and configuration:

```text
OMNIGENT_DICTATION_ENGINE=parakeet_mlx
OMNIGENT_DICTATION_MODEL=mlx-community/parakeet-tdt-0.6b-v3
OMNIGENT_DICTATION_DEVICE=metal
```

Do not put MLX into the portable `dictation` extra. Keep platform-specific dependencies isolated and validate Python 3.12 wheel compatibility before pinning.

`mlx-whisper` is credible and Apple-maintained, but its primary API transcribes a complete audio input. To produce stable live partials, Omnigent would need VAD, overlapping windows, repeated decoding, stable-prefix commitment, and duplicate suppression. It is therefore a larger first integration than `parakeet-mlx`.

### NVIDIA recommendation

Run NVIDIA engines in the standalone worker rather than the main server process.

Preferred options:

1. NeMo with `nvidia/parakeet-unified-en-0.6b` or a streaming Nemotron model when low latency and phrase boosting matter.
2. `faster-whisper` with CUDA when broad Whisper language coverage and simpler Python packaging matter.
3. Speech NIM/Riva when operational support, scalable streaming APIs, and request-time word boosting justify the proprietary container stack.

The 2026 unified Parakeet model supports offline and streaming inference down to a documented 160 ms configuration. NeMo supports decoding-time phrase boosting for CTC, RNNT, and TDT models. This is valuable for repository vocabulary, but the worker must receive only a bounded list of authorized terms, not a repository dump.

Heavy CUDA and NeMo dependencies should not enter Omnigent's base environment. Build a worker image and add health, auth, TLS, metrics, model warmup, and resource limits.

### Whisper recommendation

Use `faster-whisper` as the first Whisper adapter:

- Mature Python API.
- CPU quantization and NVIDIA CUDA support.
- Word timestamps.
- Language detection and translation.
- `initial_prompt` and `hotwords` parameters.
- Existing LocalAgreement/WhisperStreaming approaches can turn its offline windows into stable streaming output.

Whisper itself is not a native streaming model. Do not emit every independently decoded window directly as a final. Maintain an audio ring buffer, decode overlapping windows, commit only a stable common prefix, and use VAD/endpointing to finalize an utterance.

`whisper.cpp` is preferable when Omnigent wants recognition embedded directly in a native Electron/mobile shell, a single portable native binary, or Metal/Core ML without Python. For the current Python server architecture, a supervised subprocess is safer than a custom ctypes integration.

### WhisperFlow clarification

There are at least two relevant meanings:

- The open-source `dimastatz/whisper-flow` project implements incremental transcription around OpenAI Whisper.
- The WhisperFlow research work describes streaming optimization but is not an obvious maintained drop-in engine.

The open-source service's useful idea is rolling/tumbling-window incremental decoding. Its pinned web-server and ML dependency stack should not be installed directly into Omnigent. Reuse the algorithmic approach inside a focused faster-whisper adapter instead.

## Repository-aware dictation design

### Why acoustic recognition alone is insufficient

Hotwords can increase the chance that an acoustic decoder emits a term, but they do not solve all required behavior:

- Exact casing and punctuation: `ComposerMicButton.tsx`.
- Exact symbol spelling: `useWorkspaceFileSearch`.
- Ambiguous duplicate filenames or symbols.
- Web Speech results that never pass through a server engine.
- Engine portability where hotword APIs differ or do not exist.
- Safe decisions about whether a phrase refers to ordinary English or code.

Use both layers:

```text
Layer 1: optional engine hotwords for better acoustic hypotheses
Layer 2: mandatory repository resolver for exact, explainable replacement
```

### Existing primitives to reuse

The repository already contains most of the safe filesystem and UI substrate:

- Session read authorization and runner/host fallback in `omnigent/server/routes/sessions/routes_resources.py:191-223,283-366`.
- Runner workspace search in `omnigent/runner/environment_filesystem.py:375-498`.
- Host workspace search in `omnigent/workspace_fs.py:301-371`.
- Workspace path confinement in `omnigent/workspace_fs.py:80-118`.
- Git-aware repository discovery in `omnigent/runtime/filesystem_registry.py:184-227`.
- Pure fuzzy-scoring ideas in `sdks/ui/omnigent_ui_sdk/terminal/_completer.py:64-107`. Its local subprocess/file enumeration must not be reused server-side because it bypasses session authorization.
- File mention ranking and selection UI in `web/src/hooks/useMentionBrowser.ts` and `web/src/components/FileMentionMenu.tsx`.
- Slash-command vocabulary in `web/src/pages/ChatPage.tsx:3611-3643`.
- Session skills in the web chat store.

There is no reusable general LSP workspace-symbol service in the repository today.

### Resolver pipeline

For finalized transcript text only:

1. Obtain an authorized, bounded repository token catalog.
2. Generate spoken aliases for exact repository tokens.
3. Find transcript n-grams that resemble aliases.
4. Rank candidates using phonetic/edit similarity and repository context.
5. Auto-apply only high-confidence, high-margin replacements.
6. Show alternatives for ambiguous matches.
7. Preserve the raw transcript and allow one-click undo.

Examples:

```text
"composer mike button"       -> ComposerMicButton
"server dictation md"        -> server-dictation.md
"use workspace file search"  -> useWorkspaceFileSearch
"slash compact"              -> /compact
"pie project toml"           -> pyproject.toml
```

Generate aliases by splitting camelCase, PascalCase, snake_case, kebab-case, dots, path separators, acronyms, and digits. Add a small technical homophone table such as `py`/"pie", `JSON`/"Jason", and `git`/"get", but only allow replacements backed by repository tokens.

### Candidate catalog

Start with:

- Workspace-relative paths and basenames.
- Python functions and classes from `ast`.
- Conservative JavaScript/TypeScript declarations for functions, classes, interfaces, types, enums, and named exports.
- Slash commands and enabled skills.
- Dependency names, environment variable names, `just` recipes, package scripts, and common config identifiers.

Context boosts should include:

- Current open file.
- Changed files.
- Files already mentioned in the draft or conversation.
- Symbols located in those files.
- Explicit framing such as "file", "function", "class", "slash", or "environment variable".

Do not begin with a full LSP integration. A bounded Git file list plus lightweight declaration extraction is simpler, portable, and enough to evaluate value. Add LSP providers later if measured symbol recall is inadequate.

### API recommendation

First add a session-authorized resolver endpoint:

```http
POST /v1/sessions/{session_id}/dictation/resolve
```

Request:

```json
{
  "text": "update composer mike button",
  "commands": ["/compact", "/context", "/model"],
  "skills": [],
  "context": {
    "open_path": "web/src/components/ComposerMicButton.tsx",
    "mentioned_paths": []
  }
}
```

Response:

```json
{
  "text": "update ComposerMicButton",
  "replacements": [
    {
      "start": 7,
      "end": 27,
      "heard": "composer mike button",
      "replacement": "ComposerMicButton",
      "kind": "symbol",
      "confidence": 0.97,
      "path": "web/src/components/ComposerMicButton.tsx",
      "line": 89,
      "alternatives": []
    }
  ]
}
```

The frontend should call this for:

- Web Speech finals.
- Server `final` events.
- The server's final stop tail.

Keep live partials raw. Resolving them around six times per second would waste work and cause distracting rewrites.

New-chat resolution is a separate phase because no session exists yet. Add an owner-authorized host/workspace endpoint using the host selected in `NewChatDialog`, never a browser-supplied arbitrary filesystem path.

### Privacy boundary

Repository resolution should happen on the authorized main server, runner, or connected host after acoustic transcription.

Do not send the repository catalog to the remote dictation worker by default. It should remain an acoustic service even though Phase 1 authenticates and encrypts its transport. If engine-level hotwords are enabled, send only a bounded per-take term list.

Requirements:

- Enforce the same session read permission as filesystem browsing.
- Keep indexes inside the runner/host where files live.
- Never accept arbitrary workspace roots from the client.
- Do not log transcript text or repository candidate lists.
- Return only top candidates, not the whole symbol catalog.
- Recheck authorization on every lookup. Key caches by host/runner identity, tenant or principal, canonical workspace, and repository state so repository-derived data cannot cross authorization boundaries.
- Fall back to raw transcript on any resolver timeout or failure.

### Ambiguity UX

- High confidence plus a clear margin: replace automatically and show an Undo affordance.
- Medium confidence: retain raw text and display up to three candidates with token kind and path.
- Low confidence: make no change.
- Never replace a common English word only because a repository symbol sounds similar.
- Reuse the keyboard/listbox patterns from `FileMentionMenu`.

Suggested auto-apply gates for initial evaluation:

```text
top score >= 0.90
top score - second score >= 0.12
```

Tune these from a checked-in synthetic evaluation corpus, not private user transcripts.

## Recommended implementation roadmap

### Phase 1: Harden and expose current dictation

Priority: immediate.

- Add Dictation settings: preferred path (`auto`, `server only`, `browser only`), language, microphone, and privacy explanation.
- Add an engine diagnostics endpoint or authenticated settings status with engine name, model, readiness, worker health, and actionable errors.
- Add worker authentication, TLS guidance, health/readiness, warmup, and metrics.
- Fix the misleading "from anywhere" wording or implement a real Electron global shortcut.
- Insert at the caret or replace the selected composer range.
- Reuse the transcription capture stream for visualization to avoid a second microphone stream in server mode.
- Add visible/announced listening, loading, and error states rather than tooltip-only errors.

### Phase 2: Apple MLX engine

Priority: highest new engine for this machine.

- Add a platform-specific `dictation-mlx` extra.
- Implement `ParakeetMlxDictationEngine` and a per-take stream wrapper.
- Convert incoming PCM16 bytes to the model's normalized one-dimensional audio array.
- Accumulate stable and draft tokens into `partial`; emit `finalized` only when Omnigent endpointing closes a complete utterance. Token-level finality must not create one composer append per token.
- Define endpoint and finish behavior explicitly.
- Add model ID/cache configuration and availability checks.
- Add a real-audio opt-in smoke test and latency/real-time-factor benchmark.
- Validate model and code licenses in release documentation.

### Phase 3: Repository-aware final-text resolver

Priority: highest quality improvement.

- Build a pure token normalizer, alias generator, scorer, and replacement planner.
- Add session-authorized file and command resolution.
- Add lightweight Python and TypeScript/JavaScript symbol extraction.
- Resolve finals from both Web Speech and server paths.
- Add correction/undo and ambiguity UI.
- Add new-chat host/workspace support after the in-session flow is proven.

### Phase 4: faster-whisper engine

Priority: broad hardware and multilingual coverage.

- Implement an audio ring buffer, VAD, overlapping windows, and stable-prefix commitment.
- Expose model, language, task, hotwords, and initial prompt internally.
- Run CUDA configurations in a dedicated worker image.
- Keep CPU quantized models available for portable deployments.
- Add word timestamps to an optional richer protocol revision.

### Phase 5: NVIDIA worker

Priority: organizations with NVIDIA hardware or concurrency requirements.

- Implement a NeMo streaming adapter or a gRPC adapter to Speech NIM/Riva.
- Use per-stream phrase boosting with a bounded authorized vocabulary.
- Add GPU scheduling, memory admission control, batching policy, warmup, health, and observability.
- Publish a separate CUDA worker image and deployment guide.

### Phase 6: Native and hands-free features

Priority: later.

- Electron OS-global push-to-talk shortcut.
- Native whisper.cpp engine for desktop/mobile offline recognition.
- Configurable voice activity auto-stop.
- Optional "stop and send" command distinct from current "stop and keep".
- Terminal REPL dictation using the same server protocol.
- Optional voice commands and wake word only after explicit privacy and accidental-action safeguards.
- TTS and full voice conversation as a separate feature, not an extension of composer dictation.

## Protocol evolution

Preserve the existing PCM protocol for compatibility, but add an optional start control before audio:

```json
{
  "type": "start",
  "language": "en",
  "task": "transcribe",
  "hotwords": ["Omnigent", "ComposerMicButton"],
  "model": null
}
```

Add optional rich event fields without requiring clients to consume them:

```json
{
  "type": "partial",
  "text": "update composer mic button",
  "language": "en",
  "words": [
    {"text": "update", "start": 0.12, "end": 0.51, "confidence": 0.94}
  ]
}
```

Do not put session IDs, workspace roots, or complete repository catalogs into the acoustic WebSocket. Repository correction belongs at the authenticated application layer.

## Quality-of-life improvements

### User experience

- A visible prewarming state for first model load, which can currently take up to 40 seconds client-side.
- A listening timer and explicit active-engine indicator.
- Clear distinction between local server, remote worker, and browser/cloud recognition.
- Language and model selection with server-supported choices.
- Microphone device selection and permission recovery help.
- Caret-aware insertion and selection replacement.
- Undo for repository corrections.
- Configurable stop-on-silence and push-to-talk modes.
- An optional keyboard shortcut editor.
- A real OS-global shortcut only when the user opts in.

### Operator experience

- `omnigent dictation doctor` for dependencies, model files, model load, sample decode, real-time factor, and worker reachability.
- Managed model download with checksums, license display, and progress.
- Worker `/healthz`, `/readyz`, model metadata, and warmup.
- Structured metrics: active takes, queue time, first-partial latency, final latency, real-time factor, errors, and fallback count.
- Per-engine capacity rather than only a process-local WebSocket semaphore.
- Configuration reload or explicit restart messaging when changing the selected engine.
- Deployment examples for Apple local, CPU server, NVIDIA worker, and private remote worker.

## Risks and correctness issues to address

- Shared worker tokens remain valid until rotation and do not identify individual main servers.
- The capacity semaphore is per process.
- Sherpa decode is serialized even when two streams are admitted.
- A remote worker failure mid-take has no current-take fallback.
- The remote relay can expose stale state because transcript events arrive on a reader thread after `feed_pcm16()` returns.
- Model presence checks can report available before a corrupt/incompatible model is loaded.
- The model fetch script does not verify a checksum or signature.
- Chrome/Safari can use browser cloud speech without a force-server privacy control.
- Browser Web Speech still requires a separate visualization capture because it does not expose its audio stream.

Phase 1 added a regression test that preserves remote `final` events arriving immediately before `stopped`.

## Evaluation plan

### Acoustic engines

Measure on representative hardware:

- Word error rate.
- Technical-term error rate.
- Time to first partial.
- Partial stability and rewrite count.
- Endpoint-final latency.
- Real-time factor.
- CPU/GPU utilization and peak memory.
- Cold and warm model-load time.
- Concurrent-take degradation.

Use the same recorded, consented test set across sherpa, parakeet-mlx, faster-whisper, and NVIDIA engines.

### Repository resolver

Create a synthetic corpus containing spoken forms and expected exact tokens for filenames, paths, functions, classes, commands, acronyms, and ambiguous ordinary words.

Measure:

- Exact-token top-1 accuracy.
- Top-3 candidate recall.
- Entity span precision/recall.
- Automatic replacement precision.
- False automatic replacements per 10,000 ordinary words.
- Cold and warm resolver latency.
- Catalog build time and memory by repository size.

Initial release gates:

- At least 99% precision for automatic replacements.
- At most one false automatic replacement per 10,000 ordinary words.
- At least 95% top-3 recall for repository entities.
- Warm p95 resolver latency below 100 ms.
- Raw transcript always preserved on failure.

## How to verify the existing feature

### Install the current local sherpa path from this checkout

The project requires a supported Python 3.12 or newer environment. Using the repository's `uv` workflow:

```bash
uv python install 3.12
uv sync --python 3.12 --extra dictation --extra dev
pnpm install --frozen-lockfile
scripts/fetch-dictation-models.sh
```

Start the server with `uv run omnigent server`, open the web app in Firefox or use Electron so browser Web Speech cannot bypass sherpa, and confirm the larger `GET /v1/info` capability object contains:

```json
{"dictation_available": true}
```

Then click the composer microphone, confirm `/v1/dictation/stream` opens in developer tools or server logs, speak, verify partial text updates live, and stop with the button or `Cmd+Alt+V`. `Enter` should keep the dictated draft and `Escape` should restore the draft from before dictation began.

### Verify remote-browser transcription

Open the Omnigent web UI in Firefox, or another browser with Web Speech unavailable, from another device over HTTPS and sign in if authentication is enabled. Grant microphone permission, click the microphone, and verify that text appears in that browser's composer. In browser developer tools, confirm a successful `wss://<selected-omnigent-origin>/v1/dictation/stream` connection and binary audio frames sent only to that Omnigent origin. HTTPS is normally required for browser microphone access outside localhost, and the reverse proxy must support WebSocket upgrades.

### Verify worker offload

On the trusted worker:

```bash
OMNIGENT_DICTATION_ENGINE=sherpa \
uv run python -m omnigent.server.dictation_worker --host 0.0.0.0 --port 8100
```

On the main server:

```bash
OMNIGENT_DICTATION_ENGINE=remote \
OMNIGENT_DICTATION_REMOTE_URL=ws://<trusted-worker>:8100/v1/dictation/stream \
uv run omnigent server
```

Do not expose port 8100 publicly in the current implementation.

### Run focused automated tests

```bash
uv run pytest tests/server/routes/test_dictation.py tests/server/test_dictation_engine.py tests/server/test_dictation_remote.py
pnpm --dir web test -- ComposerMicButton useDictationInsert useVoiceDictationHotkey dictation
uv run pytest tests/e2e_ui/chat/test_dictation.py
```

The real sherpa smoke test skips unless the optional dependency, model files, and test WAVs are present.

## Recommended first releases

Keep the work independently releasable and measurable:

1. Security and privacy: add a server-only preference, authenticated/encrypted worker transport, health checks, and clear path disclosure.
2. Apple engine: ship and benchmark `parakeet-mlx` behind a platform-specific extra.
3. Resolver MVP: resolve finalized filenames, commands, Python symbols, and TypeScript/JavaScript symbols with strict auto-apply thresholds.
4. Correction UX: add Undo, ambiguity suggestions, caret-aware insertion, and accessible state/error feedback.
5. Operations: add the diagnostics command, model management, metrics, and deployment guides.

Together these provide truly local Apple Silicon transcription, preserve the existing remote-server scenario, and solve exact repository vocabulary without making the feature dependent on one ASR model.

## External sources

- parakeet-mlx: https://github.com/senstella/parakeet-mlx
- MLX Whisper: https://github.com/ml-explore/mlx-examples/tree/main/whisper
- faster-whisper: https://github.com/SYSTRAN/faster-whisper
- WhisperStreaming: https://github.com/ufal/whisper_streaming
- whisper.cpp: https://github.com/ggml-org/whisper.cpp
- NVIDIA unified Parakeet model: https://huggingface.co/nvidia/parakeet-unified-en-0.6b
- NVIDIA NeMo word boosting: https://docs.nvidia.com/nemo-framework/user-guide/latest/nemotoolkit/asr/asr_customization/word_boosting.html
- NVIDIA Speech NIM overview: https://docs.nvidia.com/nim/riva/asr/latest/overview.html
- Whisper-Flow: https://github.com/dimastatz/whisper-flow
