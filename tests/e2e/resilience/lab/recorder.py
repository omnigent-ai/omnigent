"""Record what the user sees during a scenario, with the faults captioned on screen.

:class:`UserViewRecorder` opens the session in a headless Chromium through the
client link, the same path a user's browser takes, and records a video of
each requested view (chat, terminal). Every fault, recovery and user action in
the lab's event log is drawn onto the page as a caption, so a clip explains
itself. The run's verdict is shown at the end.

Playwright's sync API is bound to the thread that created it, so the browser
lives on its own thread and captions reach it through a queue. Captions are
DOM-only, so they render even while the page's own network is cut.
"""

from __future__ import annotations

import contextlib
import logging
import queue
import shutil
import tempfile
import threading
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from tests.e2e.resilience.lab.events import EventLog, LabEvent

_logger = logging.getLogger(__name__)

_VIEWPORT = {"width": 1280, "height": 800}
_UI_TIMEOUT_MS = 60_000
_READY_TIMEOUT_S = 90.0
#: How long the closing verdict stays on screen.
_VERDICT_HOLD_S = 4.0
_LINKS = {"client": "browser", "host": "host", "model": "model"}

_SHOW_CAPTION = """
([text, tone, stamp]) => {
  let box = document.getElementById("__rlab_captions");
  if (!box) {
    box = document.createElement("div");
    box.id = "__rlab_captions";
    Object.assign(box.style, {
      position: "fixed", top: "10px", left: "50%", transform: "translateX(-50%)",
      zIndex: "2147483647", pointerEvents: "none", display: "flex",
      flexDirection: "column", gap: "4px", alignItems: "center", maxWidth: "92vw",
      font: "600 14px/1.4 ui-monospace, SFMono-Regular, Menlo, monospace",
    });
    document.documentElement.appendChild(box);
  }
  const colors = {fault: "#b91c1c", recover: "#15803d", user: "#1d4ed8",
                  info: "#374151", fail: "#b91c1c", pass: "#15803d"};
  const line = document.createElement("div");
  Object.assign(line.style, {
    background: colors[tone] || colors.info, color: "#fff", padding: "4px 10px",
    borderRadius: "6px", boxShadow: "0 2px 8px rgba(0,0,0,.35)", whiteSpace: "pre-wrap",
  });
  line.textContent = `${stamp}  ${text}`;
  box.appendChild(line);
  while (box.children.length > 4) box.removeChild(box.firstChild);
}
"""

_SHOW_BADGE = """
([title, startedMs]) => {
  if (document.getElementById("__rlab_badge")) return;
  const badge = document.createElement("div");
  badge.id = "__rlab_badge";
  Object.assign(badge.style, {
    position: "fixed", bottom: "10px", right: "10px", zIndex: "2147483647",
    pointerEvents: "none", background: "rgba(17,24,39,.85)", color: "#fff",
    padding: "4px 10px", borderRadius: "6px",
    font: "600 12px/1.4 ui-monospace, SFMono-Regular, Menlo, monospace",
  });
  const clock = document.createElement("span");
  badge.append(`${title}  `, clock);
  document.documentElement.appendChild(badge);
  const tick = () => { clock.textContent = `t=${((Date.now() - startedMs) / 1000).toFixed(1)}s`; };
  tick();
  setInterval(tick, 100);
}
"""

_SHOW_VERDICT = """
([lines, tone]) => {
  const panel = document.createElement("div");
  Object.assign(panel.style, {
    position: "fixed", inset: "20% 10% auto 10%", zIndex: "2147483647",
    pointerEvents: "none", background: tone === "pass" ? "#14532d" : "#7f1d1d",
    color: "#fff", padding: "16px 20px", borderRadius: "10px",
    boxShadow: "0 8px 24px rgba(0,0,0,.45)", whiteSpace: "pre-wrap",
    font: "600 15px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace",
  });
  panel.textContent = lines.join("\\n");
  document.documentElement.appendChild(panel);
}
"""


@dataclass(frozen=True)
class _Caption:
    text: str
    tone: str


def describe_event(event: LabEvent) -> _Caption | None:
    """Turn a lab event into an on-screen caption, or ``None`` for noise.

    :param event: An event from the lab's log.
    :returns: The caption, if the event is something a viewer should notice.
    """
    fields = event.fields
    if event.source == "lab":
        text = {
            "server_down": "server down" + (" (deploy)" if fields.get("graceful") else " (crash)"),
            "server_up": "server back",
            "sleep_host": "host asleep: processes frozen, network gone",
            "wake_host": "host awake: network back",
            "kill_runner": "runner killed",
        }.get(event.kind)
        if text is None:
            return None
        recover = event.kind in ("server_up", "wake_host")
        return _Caption(("■ " if recover else "▶ ") + text, "recover" if recover else "fault")
    if event.source == "user":
        return _Caption(f"👤 user: {fields.get('action', event.kind)}", "user")
    if event.source.startswith("proxy:"):
        link = _LINKS.get(event.source.split(":", 1)[1], event.source)
        if event.kind == "fault_start":
            return _Caption(f"▶ {link} link: {_fault_text(str(fields.get('fault', '')))}", "fault")
        if event.kind == "fault_end":
            return _Caption(
                f"■ {link} link: {_fault_text(str(fields.get('fault', '')))} cleared", "recover"
            )
        if event.kind == "reset" and fields.get("connections"):
            return _Caption(f"▶ {link} link: {fields['connections']} connection(s) reset", "fault")
    return None


def _fault_text(description: str) -> str:
    kind, _, rest = description.partition(" ")
    target = rest.split(" ", 1)[0] if rest else "*"
    scope = "" if target in ("*", "") else f" [{target}]"
    names = {
        "blackhole": "half-open (packets dropped)",
        "refuse": "unreachable (connections refused)",
        "delay": "slow",
        "throttle": "throttled",
        "recycle": "ingress recycling connections",
        "sever_held": "front door cutting long requests",
        "flap": "flapping",
    }
    return names.get(kind, kind) + scope


class UserViewRecorder:
    """Record the session page through the client link while a scenario runs.

    :param url: Session page URL behind the client proxy.
    :param out_dir: Where finished videos are written.
    :param stem: File name stem, e.g. ``"S2-codex-approval_pending-60s"``.
    :param title: Badge text shown in the corner of every frame.
    :param events: Lab event log whose faults become captions.
    :param views: ``"chat"`` and/or ``"terminal"``; one video per view.
    """

    def __init__(
        self,
        url: str,
        out_dir: Path,
        *,
        stem: str,
        title: str,
        events: EventLog,
        views: Sequence[str] = ("chat",),
    ) -> None:
        self._url = url
        self._out_dir = out_dir
        self._stem = stem
        self._title = title
        self._events = events
        self._views = tuple(views)
        self._queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._videos: list[Path] = []
        self._started = time.time()
        self._thread = threading.Thread(target=self._run, name="resilience-recorder", daemon=True)
        self._unsubscribe: object = None

    def start(self) -> UserViewRecorder:
        """Open the page, start recording and captioning; returns ``self``.

        :raises RuntimeError: When the page cannot be opened.
        """
        self._thread.start()
        if not self._ready.wait(_READY_TIMEOUT_S) or self._error is not None:
            self._queue.put(("stop", None))
            raise RuntimeError(f"recorder could not open {self._url}: {self._error}")
        self._unsubscribe = self._events.subscribe(self._on_event)
        return self

    def caption(self, text: str, tone: str = "info") -> None:
        """Show *text* on screen now; *tone* is ``info``, ``fault``, ``recover`` or ``user``."""
        self._queue.put(("caption", _Caption(text, tone)))

    def stop(self, verdict: Iterable[str] = (), *, passed: bool = True) -> list[Path]:
        """Show *verdict* briefly, stop recording and return the saved videos."""
        if callable(self._unsubscribe):
            self._unsubscribe()
        self._queue.put(("verdict", (list(verdict), "pass" if passed else "fail")))
        self._queue.put(("stop", None))
        self._thread.join(timeout=120)
        return list(self._videos)

    def _on_event(self, event: LabEvent) -> None:
        caption = describe_event(event)
        if caption is not None:
            self._queue.put(("caption", caption))

    def _stamp(self) -> str:
        return f"t={time.time() - self._started:5.1f}s"

    def _run(self) -> None:
        from playwright.sync_api import sync_playwright

        record_dir = Path(tempfile.mkdtemp(prefix="rlab-video-"))
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                context = browser.new_context(
                    viewport=_VIEWPORT,
                    record_video_dir=str(record_dir),
                    record_video_size=_VIEWPORT,
                )
                pages = []
                try:
                    for view in self._views:
                        page = context.new_page()
                        page.goto(self._url)
                        _prepare(page, view)
                        page.evaluate(
                            _SHOW_BADGE, [f"{self._title} · {view}", self._started * 1000]
                        )
                        pages.append((view, page))
                except BaseException as exc:
                    self._error = exc
                    self._ready.set()
                    context.close()
                    browser.close()
                    return
                self._ready.set()
                self._pump([page for _, page in pages])
                videos = [(view, page.video) for view, page in pages]
                context.close()
                self._out_dir.mkdir(parents=True, exist_ok=True)
                for view, video in videos:
                    if video is None:
                        continue
                    target = self._out_dir / f"{self._stem}-{view}.webm"
                    video.save_as(str(target))
                    self._videos.append(target)
                browser.close()
        except BaseException as exc:  # recording must never fail the scenario
            _logger.warning("resilience recorder failed: %s", exc, exc_info=True)
            self._error = exc
            self._ready.set()
        finally:
            shutil.rmtree(record_dir, ignore_errors=True)

    def _pump(self, pages: list[object]) -> None:
        last_sweep = 0.0
        while True:
            if time.monotonic() - last_sweep > 1.0:
                last_sweep = time.monotonic()
                for page in pages:
                    _dismiss_lab_noise(page)
            try:
                kind, payload = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if kind == "stop":
                return
            for page in pages:
                with contextlib.suppress(Exception):
                    if kind == "caption":
                        assert isinstance(payload, _Caption)
                        page.evaluate(_SHOW_CAPTION, [payload.text, payload.tone, self._stamp()])  # type: ignore[attr-defined]
                    elif kind == "verdict":
                        page.evaluate(_SHOW_VERDICT, list(payload))  # type: ignore[attr-defined, arg-type]
            if kind == "verdict":
                time.sleep(_VERDICT_HOLD_S)


#: Dialogs that only exist because the lab is a fresh install, with the button
#: that dismisses each. Anything else stays on screen; it may be the point.
_LAB_NOISE_DIALOGS = (
    ("Your imports are ready", "Close"),
    ("copying from terminals", "Block"),
)


def _dismiss_lab_noise(page: object) -> None:
    for text, button in _LAB_NOISE_DIALOGS:
        with contextlib.suppress(Exception):
            dialog = page.get_by_role("dialog").filter(has_text=text)  # type: ignore[attr-defined]
            if dialog.count():
                dialog.get_by_role("button", name=button, exact=True).first.click(timeout=2000)


def _prepare(page: object, view: str) -> None:
    """Dismiss first-run dialogs and switch the session to *view*."""
    from playwright.sync_api import expect

    toggle = page.get_by_test_id("view-mode-toggle")  # type: ignore[attr-defined]
    expect(toggle).to_be_visible(timeout=_UI_TIMEOUT_MS)
    _dismiss_lab_noise(page)
    target = page.get_by_test_id(f"view-mode-{view}")  # type: ignore[attr-defined]
    if target.get_attribute("aria-pressed") != "true":
        target.click()
