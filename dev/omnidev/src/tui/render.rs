//! Frame rendering. Minimal chrome: no boxes — regions are separated by a
//! light neutral background bar instead. The header and footer share the
//! "chrome" bar; the log body sits on the terminal's default background so
//! ANSI log colors render naturally on either a light or dark theme.

use ansi_to_tui::IntoText;
use ratatui::layout::{Alignment, Constraint, Direction, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Paragraph, Tabs};
use ratatui::Frame;

use super::{App, View};
use crate::state::{ProcId, ProcStatus};

// Palette calibrated (Solarized accents) to stay legible on both light and
// dark terminals. The chrome bars use a light neutral background with dark
// text; the log body keeps the terminal default background so ANSI log colors
// render naturally on either theme. Accent hues are mid-tone so they read on
// the light bar and on both a black and a white body background.
const CHROME_BG: Color = Color::Rgb(238, 232, 213); // light neutral bar
const CHROME_FG: Color = Color::Rgb(60, 70, 72); // dark text on the bar
const MUTED: Color = Color::Rgb(120, 132, 133); // de-emphasized labels

const SERVER: Color = Color::Rgb(38, 139, 210); // blue
const HOST: Color = Color::Rgb(42, 161, 152); // cyan
const VITE: Color = Color::Rgb(211, 54, 130); // magenta
const EVENT: Color = Color::Rgb(181, 137, 0); // amber (omnidev channel)

const OK: Color = Color::Rgb(133, 153, 0); // green (running)
const WARN: Color = Color::Rgb(203, 75, 22); // orange (starting/restarting)
const ERR: Color = Color::Rgb(220, 50, 47); // red (crashed)

/// Style for the header/footer chrome bars.
fn chrome() -> Style {
    Style::default().bg(CHROME_BG).fg(CHROME_FG)
}

pub fn draw(f: &mut Frame, app: &App) {
    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(1), // pod path
            Constraint::Length(1), // urls
            Constraint::Length(1), // status chips
            Constraint::Length(1), // tabs + scroll status
            Constraint::Min(1),    // body
            Constraint::Length(1), // footer
        ])
        .split(f.area());

    draw_pod(f, app, chunks[0]);
    draw_urls(f, app, chunks[1]);
    draw_chips(f, app, chunks[2]);
    draw_tabs_row(f, app, chunks[3]);
    draw_body(f, app, chunks[4]);
    draw_footer(f, chunks[5]);
}

fn draw_pod(f: &mut Frame, app: &App, area: Rect) {
    let line = Line::from(vec![
        Span::styled(" pod ", Style::default().fg(MUTED)),
        Span::raw(app.pod.dir.display().to_string()),
    ]);
    f.render_widget(Paragraph::new(line).style(chrome()), area);
}

fn draw_urls(f: &mut Frame, app: &App, area: Rect) {
    let line = Line::from(vec![
        Span::styled(" server ", Style::default().fg(MUTED)),
        Span::styled(
            app.pod.server_display_url(),
            Style::default().fg(proc_color(ProcId::Server)),
        ),
        Span::styled("   ui ", Style::default().fg(MUTED)),
        Span::styled(
            app.pod.vite_display_url(),
            Style::default().fg(proc_color(ProcId::Vite)),
        ),
    ]);
    f.render_widget(Paragraph::new(line).style(chrome()), area);
}

fn draw_chips(f: &mut Frame, app: &App, area: Rect) {
    let status = app.shared.lock().unwrap().status.clone();
    let mut chips: Vec<Span> = vec![Span::raw(" ")];
    for id in ProcId::ALL {
        let st = &status[id.idx()];
        chips.push(Span::styled(
            id.label(),
            Style::default()
                .fg(proc_color(id))
                .add_modifier(Modifier::BOLD),
        ));
        chips.push(Span::raw(" "));
        chips.push(Span::styled(
            st.short(),
            Style::default().fg(status_color(st)),
        ));
        chips.push(Span::raw("   "));
    }
    f.render_widget(Paragraph::new(Line::from(chips)).style(chrome()), area);
}

fn draw_tabs_row(f: &mut Frame, app: &App, area: Rect) {
    // Split the row: tabs on the left, scroll/follow status right-aligned.
    let cols = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([Constraint::Min(0), Constraint::Length(24)])
        .split(area);

    let entries = [
        ("server", View::Server, Some(ProcId::Server)),
        ("host", View::Host, Some(ProcId::Host)),
        ("vite", View::Vite, Some(ProcId::Vite)),
        ("all", View::All, None),
    ];
    let selected = entries
        .iter()
        .position(|(_, v, _)| *v == app.view)
        .unwrap_or(3);
    let titles: Vec<Line> = entries
        .iter()
        .map(|(name, _, id)| {
            let color = id.map(proc_color).unwrap_or(CHROME_FG);
            Line::from(Span::styled(*name, Style::default().fg(color)))
        })
        .collect();
    let tabs = Tabs::new(titles)
        .select(selected)
        .style(chrome())
        .divider(Span::styled("·", Style::default().fg(MUTED)))
        .highlight_style(Style::default().add_modifier(Modifier::REVERSED | Modifier::BOLD));
    f.render_widget(tabs, cols[0]);

    let total = app.line_count();
    let status = if app.follow {
        format!("{total} ln · follow ")
    } else {
        format!("{total} ln · ↑{} ", app.scroll_back)
    };
    f.render_widget(
        Paragraph::new(Line::from(Span::styled(status, Style::default().fg(MUTED))))
            .alignment(Alignment::Right)
            .style(chrome()),
        cols[1],
    );
}

fn draw_body(f: &mut Frame, app: &App, area: Rect) {
    let all_view = app.view == View::All;
    let shared = app.shared.lock().unwrap();
    let lines: Vec<String> = match app.view {
        View::Server => shared.buf(ProcId::Server).iter().cloned().collect(),
        View::Host => shared.buf(ProcId::Host).iter().cloned().collect(),
        View::Vite => shared.buf(ProcId::Vite).iter().cloned().collect(),
        View::All => shared.all.iter().cloned().collect(),
    };
    drop(shared);

    let height = area.height as usize;
    let total = lines.len();
    let max_back = total.saturating_sub(height);
    let back = app.scroll_back.min(max_back);
    let end = total.saturating_sub(back);
    let start = end.saturating_sub(height);

    let rendered: Vec<Line> = lines[start..end]
        .iter()
        .map(|l| render_line(l, all_view))
        .collect();

    f.render_widget(Paragraph::new(rendered), area);
}

fn draw_footer(f: &mut Frame, area: Rect) {
    let hint = " 1/2/3/0 view · Tab cycle · ↑↓/PgUp/PgDn scroll · f follow · r restart · R backend · c clear · q quit ";
    f.render_widget(
        Paragraph::new(Line::from(Span::styled(
            hint,
            Style::default().fg(CHROME_FG),
        )))
        .style(chrome()),
        area,
    );
}

/// Turn one stored log line into a styled `Line`. In the combined view the
/// leading `[service]` tag is colored per service and the rest keeps its ANSI
/// colors; per-service panes just pass their ANSI through.
fn render_line(raw: &str, all_view: bool) -> Line<'static> {
    if all_view {
        if let Some(rest) = raw.strip_prefix('[') {
            if let Some(end) = rest.find(']') {
                let label = &rest[..end];
                let body = &rest[end + 1..];
                let mut spans = vec![Span::styled(
                    format!("[{label}]"),
                    Style::default()
                        .fg(label_color(label))
                        .add_modifier(Modifier::BOLD),
                )];
                spans.extend(ansi_spans(body));
                return Line::from(spans);
            }
        }
    }
    Line::from(ansi_spans(raw))
}

/// Parse a single line of possibly-ANSI text into owned spans, falling back to
/// the raw string if it doesn't parse.
fn ansi_spans(s: &str) -> Vec<Span<'static>> {
    match s.into_text() {
        Ok(text) => text
            .lines
            .into_iter()
            .next()
            .map(|l| l.spans)
            .unwrap_or_default(),
        Err(_) => vec![Span::raw(s.to_string())],
    }
}

fn proc_color(id: ProcId) -> Color {
    match id {
        ProcId::Server => SERVER,
        ProcId::Host => HOST,
        ProcId::Vite => VITE,
    }
}

/// Color for a `[label]` prefix in the combined view — the three services plus
/// the synthetic "omnidev" supervisor channel.
fn label_color(label: &str) -> Color {
    match label {
        "server" => proc_color(ProcId::Server),
        "host" => proc_color(ProcId::Host),
        "vite" => proc_color(ProcId::Vite),
        "omnidev" => EVENT,
        _ => MUTED,
    }
}

fn status_color(st: &ProcStatus) -> Color {
    match st {
        ProcStatus::Running(_) => OK,
        ProcStatus::Starting | ProcStatus::Restarting => WARN,
        ProcStatus::Crashed => ERR,
        ProcStatus::Stopped => VITE,
        ProcStatus::Idle => MUTED,
    }
}
