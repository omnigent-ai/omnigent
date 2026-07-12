//! Terminal UI: renders pod status + per-process log panes and turns key
//! presses into supervisor commands.

mod render;

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
    /// Lines scrolled up from the bottom; 0 == pinned to tail.
    scroll_back: usize,
    follow: bool,
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
        let page = 20;
        match (key.code, key.modifiers) {
            (KeyCode::Char('c'), KeyModifiers::CONTROL) => self.should_quit = true,
            (KeyCode::Char('q'), _) => self.should_quit = true,

            (KeyCode::Char('1'), _) => self.set_view(View::Server),
            (KeyCode::Char('2'), _) => self.set_view(View::Host),
            (KeyCode::Char('3'), _) => self.set_view(View::Vite),
            (KeyCode::Char('0'), _) => self.set_view(View::All),
            (KeyCode::Tab, _) => self.cycle_view(),

            (KeyCode::Up, _) => self.scroll(1),
            (KeyCode::Down, _) => self.scroll_down(1),
            (KeyCode::PageUp, _) => self.scroll(page),
            (KeyCode::PageDown, _) => self.scroll_down(page),

            (KeyCode::Char('f'), _) => {
                self.follow = !self.follow;
                if self.follow {
                    self.scroll_back = 0;
                }
            }
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

    fn set_view(&mut self, v: View) {
        self.view = v;
        self.scroll_back = 0;
    }

    fn cycle_view(&mut self) {
        self.view = match self.view {
            View::All => View::Server,
            View::Server => View::Host,
            View::Host => View::Vite,
            View::Vite => View::All,
        };
        self.scroll_back = 0;
    }

    fn scroll(&mut self, n: usize) {
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

    fn clear_current(&mut self) {
        let mut s = self.shared.lock().unwrap();
        match self.view {
            View::Server => s.server.clear(),
            View::Host => s.host.clear(),
            View::Vite => s.vite.clear(),
            View::All => s.all.clear(),
        }
        self.scroll_back = 0;
    }

    /// Total line count of the focused channel, for the status readout.
    pub fn line_count(&self) -> usize {
        let s = self.shared.lock().unwrap();
        match self.view {
            View::Server => s.buf(ProcId::Server).iter().count(),
            View::Host => s.buf(ProcId::Host).iter().count(),
            View::Vite => s.buf(ProcId::Vite).iter().count(),
            View::All => s.all.iter().count(),
        }
    }
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
