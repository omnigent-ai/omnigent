//! Terminal UI: renders pod status + per-process log panes and turns key
//! presses into supervisor commands.

mod render;

use std::cell::Cell;
use std::io::{self, Stdout};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use anyhow::Result;
use crossterm::event::{self, Event, KeyCode, KeyEvent, KeyEventKind, KeyModifiers};
use crossterm::execute;
use crossterm::terminal::{
    disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen,
};
use ratatui::backend::CrosstermBackend;
use ratatui::Terminal;
use tokio::sync::mpsc;

use crate::pod::Pod;
use crate::state::{ProcId, Shared};
use crate::supervisor::Cmd;

/// Which log channel is focused. `All` is the combined, source-tagged view.
#[derive(Clone, Copy, PartialEq, Eq)]
pub enum View {
    Server,
    Host,
    Vite,
    All,
}

/// Search direction. `Fwd` scans toward the tail (newer lines), `Back` toward
/// the head — matching `less`'s `/` and `?`.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Dir {
    Fwd,
    Back,
}

impl Dir {
    fn flip(self) -> Dir {
        match self {
            Dir::Fwd => Dir::Back,
            Dir::Back => Dir::Fwd,
        }
    }
}

/// A committed search: the query and the direction it was entered with.
pub struct Search {
    pub query: String,
    pub dir: Dir,
}

/// The line-editor state while the user is typing a `/` or `?` query.
pub struct InputMode {
    pub dir: Dir,
    pub query: String,
}

impl View {
    fn proc(self) -> Option<ProcId> {
        match self {
            View::Server => Some(ProcId::Server),
            View::Host => Some(ProcId::Host),
            View::Vite => Some(ProcId::Vite),
            View::All => None,
        }
    }
}

pub struct App {
    pod: Arc<Pod>,
    shared: Arc<Mutex<Shared>>,
    cmds: mpsc::UnboundedSender<Cmd>,
    view: View,
    /// Display rows scrolled up from the bottom; 0 == pinned to tail. Counted in
    /// *rendered rows*, so it stays correct whether or not lines wrap.
    scroll_back: usize,
    follow: bool,
    /// Wrap long lines to the next row (default) vs. clip them at the edge.
    wrap: bool,
    /// Body size in rows/cols, refreshed by the renderer each frame so key
    /// handling can page by a full/half window and lay out wraps for search.
    /// Seeded so keys pressed before the first draw still behave.
    viewport_h: Cell<usize>,
    viewport_w: Cell<usize>,
    /// The last committed search, if any (drives `n`/`N` and highlighting).
    search: Option<Search>,
    /// Logical line index of the match `n`/`N` last jumped to, for anchoring.
    current_match: Option<usize>,
    /// Set while the user is typing a query; steals keys from command mode.
    input: Option<InputMode>,
    should_quit: bool,
}

impl App {
    pub fn new(pod: Arc<Pod>, shared: Arc<Mutex<Shared>>, cmds: mpsc::UnboundedSender<Cmd>) -> App {
        App {
            pod,
            shared,
            cmds,
            view: View::All,
            scroll_back: 0,
            follow: true,
            wrap: true,
            viewport_h: Cell::new(20),
            viewport_w: Cell::new(80),
            search: None,
            current_match: None,
            input: None,
            should_quit: false,
        }
    }

    /// Run the render + input loop until the user quits. On return, the caller
    /// sends `Shutdown` and the terminal is already restored.
    pub async fn run(mut self) -> Result<()> {
        let mut terminal = setup_terminal()?;
        let mut input = spawn_input();
        let mut tick = tokio::time::interval(Duration::from_millis(80));

        let result = loop {
            if let Err(e) = terminal.draw(|f| render::draw(f, &self)) {
                break Err(e.into());
            }
            if self.should_quit {
                break Ok(());
            }
            tokio::select! {
                _ = tick.tick() => {}
                key = input.recv() => {
                    match key {
                        Some(key) => self.on_key(key),
                        None => break Ok(()),
                    }
                }
            }
        };

        restore_terminal(&mut terminal);
        result
    }

    fn on_key(&mut self, key: KeyEvent) {
        if key.kind != KeyEventKind::Press {
            return;
        }
        // Ctrl-C always quits, even mid-search.
        if key.code == KeyCode::Char('c') && key.modifiers.contains(KeyModifiers::CONTROL) {
            self.should_quit = true;
            return;
        }
        // While typing a query, keys build/commit/cancel it instead of running
        // commands.
        if self.input.is_some() {
            self.on_key_input(key);
            return;
        }

        let window = self.viewport_h.get().max(1);
        let half = (window / 2).max(1);
        match (key.code, key.modifiers) {
            (KeyCode::Char('q'), _) => self.should_quit = true,

            (KeyCode::Char('1'), _) => self.set_view(View::Server),
            (KeyCode::Char('2'), _) => self.set_view(View::Host),
            (KeyCode::Char('3'), _) => self.set_view(View::Vite),
            (KeyCode::Char('0'), _) => self.set_view(View::All),
            (KeyCode::Tab, _) => self.cycle_view(),

            // Pager movement — full `less` semantics.
            (KeyCode::Char('j'), _) | (KeyCode::Down, _) => self.scroll_down(1),
            (KeyCode::Char('k'), _) | (KeyCode::Up, _) => self.scroll_up(1),
            (KeyCode::Char('f'), _) | (KeyCode::Char(' '), _) | (KeyCode::PageDown, _) => {
                self.scroll_down(window)
            }
            (KeyCode::Char('b'), _) | (KeyCode::PageUp, _) => self.scroll_up(window),
            (KeyCode::Char('d'), _) => self.scroll_down(half),
            (KeyCode::Char('u'), _) => self.scroll_up(half),
            (KeyCode::Char('g'), _) | (KeyCode::Home, _) => self.scroll_to_top(),
            (KeyCode::Char('G'), _) | (KeyCode::End, _) => self.scroll_to_bottom(),

            // `less +F`: capital F toggles tail-follow.
            (KeyCode::Char('F'), _) => {
                self.follow = !self.follow;
                if self.follow {
                    self.scroll_back = 0;
                }
            }
            (KeyCode::Char('w'), _) => self.toggle_wrap(),

            // Search.
            (KeyCode::Char('/'), _) => self.begin_search(Dir::Fwd),
            (KeyCode::Char('?'), _) => self.begin_search(Dir::Back),
            (KeyCode::Char('n'), _) => self.repeat_search(false),
            (KeyCode::Char('N'), _) => self.repeat_search(true),

            (KeyCode::Char('r'), _) => {
                if let Some(id) = self.view.proc() {
                    let _ = self.cmds.send(Cmd::Restart(id));
                } else {
                    let _ = self.cmds.send(Cmd::RestartBackend);
                }
            }
            (KeyCode::Char('R'), _) => {
                let _ = self.cmds.send(Cmd::RestartBackend);
            }
            (KeyCode::Char('c'), _) => self.clear_current(),
            _ => {}
        }
    }

    /// Handle a key while a `/` or `?` query is being typed.
    fn on_key_input(&mut self, key: KeyEvent) {
        match key.code {
            KeyCode::Enter => {
                let input = self.input.take().unwrap();
                if !input.query.is_empty() {
                    self.search = Some(Search {
                        query: input.query,
                        dir: input.dir,
                    });
                    self.current_match = None;
                    self.run_search(input.dir, true);
                }
            }
            KeyCode::Esc => self.input = None,
            KeyCode::Backspace => {
                let done = {
                    let input = self.input.as_mut().unwrap();
                    input.query.pop();
                    input.query.is_empty()
                };
                if done {
                    self.input = None;
                }
            }
            KeyCode::Char(c) if !key.modifiers.contains(KeyModifiers::CONTROL) => {
                self.input.as_mut().unwrap().query.push(c);
            }
            _ => {}
        }
    }

    fn set_view(&mut self, v: View) {
        self.view = v;
        self.scroll_back = 0;
        // Match indices are per-view; drop the anchor on switch.
        self.current_match = None;
    }

    fn cycle_view(&mut self) {
        self.view = match self.view {
            View::All => View::Server,
            View::Server => View::Host,
            View::Host => View::Vite,
            View::Vite => View::All,
        };
        self.scroll_back = 0;
        self.current_match = None;
    }

    fn scroll_up(&mut self, n: usize) {
        // Scrolling up detaches from the tail.
        self.follow = false;
        self.scroll_back = self.scroll_back.saturating_add(n);
    }

    fn scroll_down(&mut self, n: usize) {
        self.scroll_back = self.scroll_back.saturating_sub(n);
        if self.scroll_back == 0 {
            self.follow = true;
        }
    }

    fn scroll_to_top(&mut self) {
        self.follow = false;
        let lines = self.display_lines();
        let counts = self.row_counts(&lines);
        let total: usize = counts.iter().sum();
        let height = self.viewport_h.get().max(1);
        self.scroll_back = total.saturating_sub(height);
    }

    fn scroll_to_bottom(&mut self) {
        self.scroll_back = 0;
        self.follow = true;
    }

    fn toggle_wrap(&mut self) {
        self.wrap = !self.wrap;
        // Row counts change with wrap; re-anchor on the matched line if any,
        // otherwise drop to the tail so we land somewhere sane.
        match self.current_match {
            Some(idx) => {
                let lines = self.display_lines();
                self.jump_to_logical(idx, &lines);
            }
            None => self.scroll_to_bottom(),
        }
    }

    fn begin_search(&mut self, dir: Dir) {
        self.input = Some(InputMode {
            dir,
            query: String::new(),
        });
    }

    /// `n` repeats the committed search in its direction; `N` (opposite=true)
    /// reverses it.
    fn repeat_search(&mut self, opposite: bool) {
        let Some(search) = self.search.as_ref() else {
            return;
        };
        let dir = if opposite {
            search.dir.flip()
        } else {
            search.dir
        };
        self.run_search(dir, false);
    }

    /// Scan for the next match and jump to it. `fresh` anchors from the current
    /// viewport; otherwise it steps off the last matched line.
    fn run_search(&mut self, dir: Dir, fresh: bool) {
        let Some(query) = self.search.as_ref().map(|s| s.query.to_ascii_lowercase()) else {
            return;
        };
        let lines = self.display_lines();
        let n = lines.len();
        if n == 0 || query.is_empty() {
            return;
        }

        let start = if fresh {
            self.anchor(&lines, dir)
        } else {
            match self.current_match {
                Some(m) => match dir {
                    Dir::Fwd => (m + 1) % n,
                    Dir::Back => (m + n - 1) % n,
                },
                None => self.anchor(&lines, dir),
            }
        };

        // Scan every line once, wrapping around the ends.
        for k in 0..n {
            let i = match dir {
                Dir::Fwd => (start + k) % n,
                Dir::Back => (start + n - (k % n)) % n,
            };
            if lines[i].to_ascii_lowercase().contains(&query) {
                self.current_match = Some(i);
                self.jump_to_logical(i, &lines);
                return;
            }
        }
    }

    /// Displayed text (ANSI stripped, `[label]` prefix included in the combined
    /// view) for every logical line of the focused channel — the exact text the
    /// renderer shows, so search offsets and wrap counts line up.
    fn display_lines(&self) -> Vec<String> {
        let all_view = self.view == View::All;
        let s = self.shared.lock().unwrap();
        let iter: Box<dyn Iterator<Item = &String>> = match self.view {
            View::Server => Box::new(s.buf(ProcId::Server).iter()),
            View::Host => Box::new(s.buf(ProcId::Host).iter()),
            View::Vite => Box::new(s.buf(ProcId::Vite).iter()),
            View::All => Box::new(s.all.iter()),
        };
        iter.map(|l| render::display_text(l, all_view)).collect()
    }

    /// Per-line display-row counts at the current width/wrap.
    fn row_counts(&self, lines: &[String]) -> Vec<usize> {
        let width = self.viewport_w.get();
        lines
            .iter()
            .map(|t| render::row_count(t, width, self.wrap))
            .collect()
    }

    /// The logical line a fresh search should scan from: the top visible line
    /// going forward, the bottom visible line going back.
    fn anchor(&self, lines: &[String], dir: Dir) -> usize {
        let counts = self.row_counts(lines);
        let total: usize = counts.iter().sum();
        let height = self.viewport_h.get().max(1);
        let back = self.scroll_back.min(total.saturating_sub(height));
        let end = total.saturating_sub(back); // one past the bottom visible row
        let top_row = end.saturating_sub(height);
        match dir {
            Dir::Fwd => line_at_row(&counts, top_row),
            Dir::Back => line_at_row(&counts, end.saturating_sub(1)),
        }
    }

    /// Scroll so logical line `idx`'s first display row sits at the top of the
    /// viewport (clamped so we never scroll past the tail).
    fn jump_to_logical(&mut self, idx: usize, lines: &[String]) {
        let counts = self.row_counts(lines);
        if idx >= counts.len() {
            return;
        }
        let height = self.viewport_h.get().max(1);
        let below: usize = counts[idx + 1..].iter().sum();
        let own = counts[idx];
        let total: usize = counts.iter().sum();
        let max_back = total.saturating_sub(height);
        self.scroll_back = (own + below).saturating_sub(height).min(max_back);
        self.follow = false;
    }

    fn clear_current(&mut self) {
        let mut s = self.shared.lock().unwrap();
        match self.view {
            View::Server => s.server.clear(),
            View::Host => s.host.clear(),
            View::Vite => s.vite.clear(),
            View::All => s.all.clear(),
        }
        self.scroll_back = 0;
        self.current_match = None;
    }

    /// Total logical line count of the focused channel, for the status readout.
    pub fn line_count(&self) -> usize {
        let s = self.shared.lock().unwrap();
        match self.view {
            View::Server => s.buf(ProcId::Server).iter().count(),
            View::Host => s.buf(ProcId::Host).iter().count(),
            View::Vite => s.buf(ProcId::Vite).iter().count(),
            View::All => s.all.iter().count(),
        }
    }

    /// The committed query, ASCII-lowercased, for the renderer's highlight
    /// pass. `None` when no search is active.
    pub fn search_query_lower(&self) -> Option<String> {
        self.search
            .as_ref()
            .filter(|s| !s.query.is_empty())
            .map(|s| s.query.to_ascii_lowercase())
    }

    /// The in-progress query prompt (`dir`, text) while the user is typing.
    pub fn input_prompt(&self) -> Option<(Dir, &str)> {
        self.input.as_ref().map(|i| (i.dir, i.query.as_str()))
    }

    /// Number of logical lines matching the committed search, for the status
    /// readout, plus the 1-based rank of the current match within them.
    pub fn match_stats(&self) -> Option<(usize, usize)> {
        let query = self.search.as_ref()?.query.to_ascii_lowercase();
        if query.is_empty() {
            return None;
        }
        let lines = self.display_lines();
        let mut total = 0;
        let mut rank = 0;
        for (i, l) in lines.iter().enumerate() {
            if l.to_ascii_lowercase().contains(&query) {
                total += 1;
                if Some(i) == self.current_match {
                    rank = total;
                }
            }
        }
        Some((rank, total))
    }
}

/// Map a display-row index to the logical line that contains it.
fn line_at_row(counts: &[usize], target_row: usize) -> usize {
    let mut acc = 0;
    for (i, &rc) in counts.iter().enumerate() {
        if target_row < acc + rc {
            return i;
        }
        acc += rc;
    }
    counts.len().saturating_sub(1)
}

fn setup_terminal() -> Result<Terminal<CrosstermBackend<Stdout>>> {
    enable_raw_mode()?;
    let mut stdout = io::stdout();
    execute!(stdout, EnterAlternateScreen)?;
    Ok(Terminal::new(CrosstermBackend::new(stdout))?)
}

fn restore_terminal(terminal: &mut Terminal<CrosstermBackend<Stdout>>) {
    let _ = disable_raw_mode();
    let _ = execute!(terminal.backend_mut(), LeaveAlternateScreen);
    let _ = terminal.show_cursor();
}

/// Read crossterm key events on a dedicated thread and forward them; the async
/// loop selects on this alongside the render tick.
fn spawn_input() -> mpsc::UnboundedReceiver<KeyEvent> {
    let (tx, rx) = mpsc::unbounded_channel();
    std::thread::spawn(move || loop {
        if event::poll(Duration::from_millis(200)).unwrap_or(false) {
            if let Ok(Event::Key(key)) = event::read() {
                if tx.send(key).is_err() {
                    break;
                }
            }
        }
    });
    rx
}

#[cfg(test)]
mod tests {
    //! Headless end-to-end: drive the real `on_key` and render through
    //! ratatui's `TestBackend`, so the full key → state → draw path is
    //! exercised without a TTY or a live pod.
    use super::*;
    use crate::ports::Ports;
    use ratatui::backend::TestBackend;
    use ratatui::Terminal;

    /// Build an `App` over a throwaway pod and a channel whose receiver we keep
    /// so `cmds.send` never fails.
    fn app() -> (App, mpsc::UnboundedReceiver<Cmd>) {
        let root = std::env::temp_dir().join(format!("omnidev-tui-{}", std::process::id()));
        let dir = root.join("pod");
        let pod = Arc::new(
            Pod::create(
                root.clone(),
                dir,
                Ports {
                    server: 6767,
                    vite: 5173,
                },
                "127.0.0.1".into(),
                Vec::new(),
            )
            .unwrap(),
        );
        let shared = Shared::new(&pod);
        let (tx, rx) = mpsc::unbounded_channel();
        (App::new(pod, shared, tx), rx)
    }

    fn press(app: &mut App, code: KeyCode) {
        app.on_key(KeyEvent::new(code, KeyModifiers::NONE));
    }

    fn type_str(app: &mut App, s: &str) {
        for c in s.chars() {
            press(app, KeyCode::Char(c));
        }
    }

    /// Render one frame at the given size and return the body rows (everything
    /// between the 4 header rows and the footer) as trimmed strings.
    fn body(app: &App, w: u16, h: u16) -> Vec<String> {
        let mut term = Terminal::new(TestBackend::new(w, h)).unwrap();
        term.draw(|f| render::draw(f, app)).unwrap();
        let buf = term.backend().buffer().clone();
        let mut rows = Vec::new();
        // Layout: 4 header rows, body fills the middle, 1 footer row.
        for y in 4..h - 1 {
            let mut s = String::new();
            for x in 0..w {
                s.push_str(buf.cell((x, y)).unwrap().symbol());
            }
            rows.push(s.trim_end().to_string());
        }
        rows
    }

    fn seed(app: &App, n: usize) {
        let mut s = app.shared.lock().unwrap();
        for i in 0..n {
            s.all.push(format!("line{i:03}"));
        }
    }

    #[test]
    fn renders_tail_by_default() {
        let (app, _rx) = app();
        seed(&app, 100);
        let rows = body(&app, 40, 12); // 4 header + 7 body + 1 footer
        assert_eq!(rows.last().unwrap(), "line099");
        assert!(rows.iter().any(|r| r == "line093"));
    }

    #[test]
    fn paging_and_ends_move_the_window() {
        let (mut app, _rx) = app();
        seed(&app, 100);
        // Establish viewport height via a first render (7 body rows).
        let _ = body(&app, 40, 12);
        press(&mut app, KeyCode::Char('b')); // page back one window
        assert!(!app.follow);
        let rows = body(&app, 40, 12);
        assert_eq!(rows.last().unwrap(), "line092");

        press(&mut app, KeyCode::Char('g')); // top
        let rows = body(&app, 40, 12);
        assert_eq!(rows.first().unwrap(), "line000");

        press(&mut app, KeyCode::Char('G')); // bottom + follow
        assert!(app.follow);
        let rows = body(&app, 40, 12);
        assert_eq!(rows.last().unwrap(), "line099");
    }

    #[test]
    fn wrap_toggle_changes_row_shape() {
        let (mut app, _rx) = app();
        {
            let mut s = app.shared.lock().unwrap();
            s.all.push("X".repeat(30)); // wider than a 10-col body
        }
        // Default wrap ON: the 30-char line occupies multiple body rows.
        let wrapped = body(&app, 10, 8);
        let nonblank = wrapped.iter().filter(|r| !r.is_empty()).count();
        assert!(nonblank >= 3, "expected wrap across rows, got {wrapped:?}");

        press(&mut app, KeyCode::Char('w')); // wrap OFF → clipped to one row
        let clipped = body(&app, 10, 8);
        let nonblank = clipped.iter().filter(|r| !r.is_empty()).count();
        assert_eq!(nonblank, 1);
    }

    #[test]
    fn search_jumps_and_highlights() {
        let (mut app, _rx) = app();
        {
            let mut s = app.shared.lock().unwrap();
            for i in 0..100 {
                let tag = if i == 5 { " ERROR here" } else { "" };
                s.all.push(format!("line{i:03}{tag}"));
            }
        }
        let _ = body(&app, 40, 12);
        // `/error` + Enter jumps up to the match near the top of the body.
        press(&mut app, KeyCode::Char('/'));
        type_str(&mut app, "error");
        press(&mut app, KeyCode::Enter);
        assert_eq!(app.current_match, Some(5));
        assert_eq!(app.match_stats(), Some((1, 1)));

        // The matched line is visible and its "ERROR" is highlighted.
        let mut term = Terminal::new(TestBackend::new(40, 12)).unwrap();
        term.draw(|f| render::draw(f, &app)).unwrap();
        let buf = term.backend().buffer().clone();
        let mut highlit = 0;
        for y in 4..11 {
            for x in 0..40 {
                let cell = buf.cell((x, y)).unwrap();
                let is_match_char = matches!(cell.symbol(), "E" | "R" | "O");
                if is_match_char && cell.bg == render::match_bg() {
                    highlit += 1;
                }
            }
        }
        assert!(
            highlit >= 5,
            "expected the match highlighted, got {highlit}"
        );
    }

    #[test]
    fn typing_query_does_not_run_commands() {
        let (mut app, _rx) = app();
        seed(&app, 100);
        let _ = body(&app, 40, 12);
        press(&mut app, KeyCode::Char('/'));
        // 'q' would quit in command mode; here it's just query text.
        type_str(&mut app, "q");
        assert!(!app.should_quit);
        assert_eq!(app.input_prompt(), Some((Dir::Fwd, "q")));
        press(&mut app, KeyCode::Esc);
        assert!(app.input_prompt().is_none());
    }
}
