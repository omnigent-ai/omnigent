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
use unicode_width::UnicodeWidthChar;

use super::{App, Dir, View};
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
const LABEL_WIDTH: usize = 7;

const OK: Color = Color::Rgb(133, 153, 0); // green (running)
const WARN: Color = Color::Rgb(203, 75, 22); // orange (starting/restarting)
const ERR: Color = Color::Rgb(220, 50, 47); // red (crashed)

// Search-match highlight: amber background with near-black text, legible on
// either theme and distinct from the ANSI log colors underneath.
const MATCH_BG: Color = Color::Rgb(181, 137, 0);
const MATCH_FG: Color = Color::Rgb(20, 20, 20);

/// Style for the header/footer chrome bars.
fn chrome() -> Style {
    Style::default().bg(CHROME_BG).fg(CHROME_FG)
}

/// The search-match background, exposed for tests that assert highlighting.
#[cfg(test)]
pub fn match_bg() -> Color {
    MATCH_BG
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
    draw_footer(f, app, chunks[5]);
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
        .constraints([Constraint::Min(0), Constraint::Length(36)])
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
    let mut status = format!("{total} ln");
    if !app.wrap {
        status.push_str(" · nowrap");
    }
    if let Some((rank, count)) = app.match_stats() {
        status.push_str(&format!(" · {rank}/{count}"));
    }
    if app.follow {
        status.push_str(" · follow ");
    } else {
        status.push_str(&format!(" · ↑{} ", app.scroll_back));
    }
    f.render_widget(
        Paragraph::new(Line::from(Span::styled(status, Style::default().fg(MUTED))))
            .alignment(Alignment::Right)
            .style(chrome()),
        cols[1],
    );
}

fn draw_body(f: &mut Frame, app: &App, area: Rect) {
    let all_view = app.view == View::All;
    let width = area.width as usize;
    let height = area.height as usize;
    // Publish the body geometry so key handling can page and search can wrap.
    app.viewport_h.set(height);
    app.viewport_w.set(width);

    let shared = app.shared.lock().unwrap();
    let lines: Vec<String> = match app.view {
        View::Server => shared.buf(ProcId::Server).iter().cloned().collect(),
        View::Host => shared.buf(ProcId::Host).iter().cloned().collect(),
        View::Vite => shared.buf(ProcId::Vite).iter().cloned().collect(),
        View::All => shared.all.iter().cloned().collect(),
    };
    drop(shared);

    let query = app.search_query_lower();
    let visible = visible_rows(
        &lines,
        all_view,
        width,
        height,
        app.wrap,
        app.scroll_back,
        query.as_deref(),
    );
    f.render_widget(Paragraph::new(visible), area);
}

/// The window of display rows to show: the `height` rows sitting `scroll_back`
/// rows above the tail. Rows are built from the bottom up, wrapping only enough
/// logical lines to cover `scroll_back + height` so a full buffer isn't
/// re-parsed every frame. Equivalent to wrapping every line and slicing the
/// flat list, but without the wasted work.
fn visible_rows(
    lines: &[String],
    all_view: bool,
    width: usize,
    height: usize,
    wrap: bool,
    scroll_back: usize,
    query: Option<&str>,
) -> Vec<Line<'static>> {
    // `acc` holds rows bottom-to-top; each logical line yields one row (wrap
    // off) or several (wrap on), so `scroll_back` counts rendered rows.
    let needed = scroll_back.saturating_add(height);
    let mut acc: Vec<Line> = Vec::with_capacity(needed + 8);
    let mut exhausted = true;
    for raw in lines.iter().rev() {
        let spans = render_line(raw, all_view);
        let ranges = query.map(|q| match_ranges(raw, all_view, q));
        let mut line_rows: Vec<Line> = Vec::new();
        wrap_spans(spans, width, wrap, ranges.as_deref(), &mut line_rows);
        acc.extend(line_rows.into_iter().rev());
        if acc.len() >= needed {
            exhausted = false;
            break;
        }
    }

    // If we ran out of lines the buffer is shorter than the scroll offset, so
    // clamp to the top; otherwise `scroll_back` is within range as-is.
    let back = if exhausted {
        scroll_back.min(acc.len().saturating_sub(height))
    } else {
        scroll_back
    };
    let end = (back + height).min(acc.len());
    let mut visible: Vec<Line> = acc.drain(back..end).collect();
    visible.reverse();
    visible
}

fn draw_footer(f: &mut Frame, app: &App, area: Rect) {
    // While typing a query the footer becomes the search prompt with a cursor
    // block; otherwise it lists the key hints.
    let line = if let Some((dir, query)) = app.input_prompt() {
        let sigil = match dir {
            Dir::Fwd => '/',
            Dir::Back => '?',
        };
        Line::from(vec![
            Span::styled(
                format!(" {sigil}{query}"),
                Style::default().fg(CHROME_FG).add_modifier(Modifier::BOLD),
            ),
            Span::styled("█", Style::default().fg(CHROME_FG)),
        ])
    } else {
        let hint = " f/b page · d/u half · j/k line · g/G ends · F follow · w wrap · / ? search · n/N next · 1230/Tab view · r/R restart · c clear · q quit ";
        Line::from(Span::styled(hint, Style::default().fg(CHROME_FG)))
    };
    f.render_widget(Paragraph::new(line).style(chrome()), area);
}

/// Turn one stored log line into styled spans. In the combined view the leading
/// `[service]` tag is colored per service and the rest keeps its ANSI colors;
/// per-service panes just pass their ANSI through.
fn render_line(raw: &str, all_view: bool) -> Vec<Span<'static>> {
    if all_view {
        if let Some(rest) = raw.strip_prefix('[') {
            if let Some(end) = rest.find(']') {
                let label = &rest[..end];
                let body = &rest[end + 1..];
                let mut spans = vec![Span::styled(
                    format!("[{label:<LABEL_WIDTH$}]"),
                    Style::default()
                        .fg(label_color(label))
                        .add_modifier(Modifier::BOLD),
                )];
                spans.extend(ansi_spans(body));
                return spans;
            }
        }
    }
    ansi_spans(raw)
}

/// The exact text `render_line` will display (ANSI stripped, `[label]` prefix
/// included), so search offsets and wrap-row counts line up with what's drawn.
pub fn display_text(raw: &str, all_view: bool) -> String {
    render_line(raw, all_view)
        .iter()
        .map(|s| s.content.as_ref())
        .collect()
}

/// Column width of a char for layout. Control and zero-width chars (including
/// tabs) count as 0 — good enough for log lines.
fn char_cols(c: char) -> usize {
    UnicodeWidthChar::width(c).unwrap_or(0)
}

/// How many display rows `text` occupies at `width` columns. Must stay in step
/// with `wrap_spans`' row splitting so scroll math and search jumps agree.
pub fn row_count(text: &str, width: usize, wrap: bool) -> usize {
    if !wrap || width == 0 {
        return 1;
    }
    let mut rows = 1;
    let mut col = 0;
    for c in text.chars() {
        let w = char_cols(c);
        if col + w > width && col > 0 {
            rows += 1;
            col = 0;
        }
        col += w;
    }
    rows
}

/// Char-offset ranges of every case-insensitive occurrence of `query` (already
/// ASCII-lowercased) in the line's displayed text. Offsets are in chars so they
/// align with `wrap_spans`' per-char highlight test.
fn match_ranges(raw: &str, all_view: bool, query: &str) -> Vec<(usize, usize)> {
    let mut ranges = Vec::new();
    if query.is_empty() {
        return ranges;
    }
    let hay: Vec<char> = display_text(raw, all_view)
        .chars()
        .map(|c| c.to_ascii_lowercase())
        .collect();
    let q: Vec<char> = query.chars().collect();
    if hay.len() < q.len() {
        return ranges;
    }
    let mut i = 0;
    while i + q.len() <= hay.len() {
        if hay[i..i + q.len()] == q[..] {
            ranges.push((i, i + q.len()));
            i += q.len();
        } else {
            i += 1;
        }
    }
    ranges
}

/// Split one logical line's spans into display rows, pushing each row onto
/// `out`. When `wrap` is off (or width 0) the line stays a single row — clipped
/// at the edge by the renderer, as before. Contiguous same-style chars coalesce
/// into one span. Chars whose char-offset falls in a `matches` range get the
/// search-highlight style overlaid, so a match spanning a wrap boundary lights
/// up on both rows.
fn wrap_spans(
    spans: Vec<Span<'static>>,
    width: usize,
    wrap: bool,
    matches: Option<&[(usize, usize)]>,
    out: &mut Vec<Line<'static>>,
) {
    let matches = matches.unwrap_or(&[]);
    // Nothing to reflow or highlight: emit the spans as one row untouched.
    if (!wrap || width == 0) && matches.is_empty() {
        out.push(Line::from(spans));
        return;
    }

    let in_match = |off: usize| matches.iter().any(|&(s, e)| off >= s && off < e);

    let mut row: Vec<Span<'static>> = Vec::new();
    let mut run = String::new();
    let mut run_style: Option<Style> = None;
    let mut col = 0usize;
    let mut offset = 0usize;

    for span in &spans {
        let base = span.style;
        for c in span.content.chars() {
            let w = char_cols(c);
            if wrap && width > 0 && col + w > width && col > 0 {
                flush_run(&mut run, run_style.unwrap_or_default(), &mut row);
                out.push(Line::from(std::mem::take(&mut row)));
                col = 0;
            }
            let style = if in_match(offset) {
                base.bg(MATCH_BG).fg(MATCH_FG).add_modifier(Modifier::BOLD)
            } else {
                base
            };
            if run_style != Some(style) {
                flush_run(&mut run, run_style.unwrap_or_default(), &mut row);
                run_style = Some(style);
            }
            run.push(c);
            col += w;
            offset += 1;
        }
    }
    flush_run(&mut run, run_style.unwrap_or_default(), &mut row);
    out.push(Line::from(row));
}

/// Emit the buffered same-style run as a span, clearing the buffer.
fn flush_run(run: &mut String, style: Style, row: &mut Vec<Span<'static>>) {
    if !run.is_empty() {
        row.push(Span::styled(std::mem::take(run), style));
    }
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

#[cfg(test)]
mod tests {
    use super::*;

    fn rows(text: &str, width: usize, wrap: bool) -> Vec<String> {
        let mut out = Vec::new();
        wrap_spans(
            vec![Span::raw(text.to_string())],
            width,
            wrap,
            None,
            &mut out,
        );
        out.iter()
            .map(|l| l.spans.iter().map(|s| s.content.as_ref()).collect())
            .collect()
    }

    #[test]
    fn wrap_off_is_one_row() {
        assert_eq!(rows("hello world", 4, false), vec!["hello world"]);
        assert_eq!(row_count("hello world", 4, false), 1);
    }

    #[test]
    fn wrap_splits_at_width_and_row_count_agrees() {
        let text = "abcdefgh";
        assert_eq!(rows(text, 3, true), vec!["abc", "def", "gh"]);
        assert_eq!(row_count(text, 3, true), 3);
    }

    #[test]
    fn wide_char_that_does_not_fit_wraps_first() {
        // "a" then a 2-wide char into width 2: the wide char can't share the
        // row with "a", so it starts the next one.
        let rows = rows("a世", 2, true);
        assert_eq!(rows, vec!["a", "世"]);
        assert_eq!(row_count("a世", 2, true), 2);
    }

    #[test]
    fn zero_width_join_does_not_add_a_row() {
        // A trailing combining mark rides the last column, not a new row.
        assert_eq!(row_count("abc\u{0301}", 3, true), 1);
    }

    #[test]
    fn width_zero_never_panics() {
        assert_eq!(rows("abc", 0, true), vec!["abc"]);
        assert_eq!(row_count("abc", 0, true), 1);
    }

    #[test]
    fn match_ranges_are_case_insensitive_char_offsets() {
        assert_eq!(
            match_ranges("Error: ERROR", false, "error"),
            vec![(0, 5), (7, 12)]
        );
        assert_eq!(match_ranges("nope", false, "error"), vec![]);
    }

    /// Reference: wrap every line into one flat list, then slice the window —
    /// the obvious-but-wasteful version `visible_rows` optimizes.
    fn naive_visible(
        lines: &[String],
        width: usize,
        height: usize,
        wrap: bool,
        scroll_back: usize,
    ) -> Vec<String> {
        let mut all: Vec<Line> = Vec::new();
        for raw in lines {
            wrap_spans(vec![Span::raw(raw.clone())], width, wrap, None, &mut all);
        }
        let total = all.len();
        let back = scroll_back.min(total.saturating_sub(height));
        let end = total.saturating_sub(back);
        let start = end.saturating_sub(height);
        all[start..end].iter().map(row_text).collect()
    }

    fn row_text(l: &Line) -> String {
        l.spans.iter().map(|s| s.content.as_ref()).collect()
    }

    fn lazy_visible(
        lines: &[String],
        width: usize,
        height: usize,
        wrap: bool,
        scroll_back: usize,
    ) -> Vec<String> {
        visible_rows(lines, false, width, height, wrap, scroll_back, None)
            .iter()
            .map(row_text)
            .collect()
    }

    #[test]
    fn lazy_slice_matches_naive_across_offsets() {
        let lines: Vec<String> = (0..30).map(|i| format!("line{i:02}=abcdefghij")).collect();
        for &wrap in &[false, true] {
            for width in [6usize, 8, 40] {
                for height in [1usize, 5, 12] {
                    for back in [0usize, 3, 10, 25, 999] {
                        assert_eq!(
                            lazy_visible(&lines, width, height, wrap, back),
                            naive_visible(&lines, width, height, wrap, back),
                            "wrap={wrap} width={width} height={height} back={back}",
                        );
                    }
                }
            }
        }
    }

    #[test]
    fn empty_and_short_buffers_do_not_panic() {
        assert!(lazy_visible(&[], 10, 5, true, 0).is_empty());
        let one = vec!["hi".to_string()];
        assert_eq!(lazy_visible(&one, 10, 5, true, 0), vec!["hi"]);
        assert_eq!(lazy_visible(&one, 10, 5, true, 99), vec!["hi"]);
    }

    #[test]
    fn highlight_survives_a_wrap_boundary() {
        // "error" at chars 2..7 straddles the width-4 wrap between rows.
        let ranges = match_ranges("--error--", false, "error");
        let mut out = Vec::new();
        wrap_spans(
            vec![Span::raw("--error--".to_string())],
            4,
            true,
            Some(&ranges),
            &mut out,
        );
        // Every row that overlaps the match must carry a highlighted span.
        let highlighted: usize = out
            .iter()
            .flat_map(|l| &l.spans)
            .filter(|s| s.style.bg == Some(MATCH_BG))
            .map(|s| s.content.chars().count())
            .sum();
        assert_eq!(highlighted, 5); // all five chars of "error"
    }
}
