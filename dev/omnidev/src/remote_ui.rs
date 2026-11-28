//! `omnidev ui` — run only this checkout's Vite UI in the foreground, proxied
//! to a remote Omnigent server given by `--server` or picked from recent ones.
//! `--prod` builds the production bundle and serves it with `vite preview`.
//!
//! Vite's dev proxy (`web/vite.config.ts`) does the server-specific work: it
//! fills in the workspace API mount, turns a `?o=<org-id>` selector into the
//! org routing header, and mints tokens from the Databricks CLI profile.

use std::io::{IsTerminal, Write};
use std::os::unix::process::CommandExt;
use std::path::Path;
use std::process::Command;

use anyhow::{bail, Context, Result};
use crossterm::cursor::MoveUp;
use crossterm::event::{self, Event, KeyCode, KeyEventKind, KeyModifiers};
use crossterm::queue;
use crossterm::terminal::{self, Clear, ClearType};
use serde::{Deserialize, Serialize};

use crate::{paths, pod};

const MAX_RECENT: usize = 10;
const PICKER_TITLE: &str = "Server (↑/↓ select, Enter start, Esc cancel):";
const NEW_URL_ROW: &str = "Enter a new URL…";

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Server {
    pub url: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub databricks_profile: Option<String>,
}

pub fn run(
    server_url: Option<String>,
    databricks_profile: Option<String>,
    prod: bool,
) -> Result<()> {
    let repo_root = paths::find_repo_root(&std::env::current_dir()?, false)?;
    let recent_path = paths::recent_servers_path()?;
    let mut recent = load_recent(&recent_path);
    let Some(url) = (match server_url {
        Some(url) => Some(url),
        None => pick(&recent)?,
    }) else {
        eprintln!("Cancelled.");
        return Ok(());
    };
    check_url(&url)?;
    // Reuse the profile last paired with this URL unless one is given; "" clears it.
    let databricks_profile = match databricks_profile {
        Some(profile) => Some(profile).filter(|profile| !profile.is_empty()),
        None => recent
            .iter()
            .find(|known| known.url == url)
            .and_then(|known| known.databricks_profile.clone()),
    };
    let server = Server {
        url,
        databricks_profile,
    };
    remember(&mut recent, server.clone());
    if let Err(error) = save_recent(&recent_path, &recent) {
        eprintln!("omnidev: could not remember {}: {error:#}", server.url);
    }

    let web = repo_root.join("web");
    if pod::web_deps_stale(&web, pod::oss_web_manifests(&repo_root)) {
        eprintln!("web deps missing or stale — running pnpm install");
        // Non-fatal like the supervisor's prepare step: Vite reports anything still missing.
        let installed = Command::new("pnpm")
            .arg("install")
            .current_dir(&web)
            .status()
            .is_ok_and(|status| status.success());
        if !installed {
            eprintln!("omnidev: pnpm install failed; starting Vite anyway");
        }
    }

    // A static token left in the shell would be sent to whichever server was picked.
    if std::env::var_os("OMNIGENT_AUTH_TOKEN").is_some() {
        eprintln!("omnidev: ignoring OMNIGENT_AUTH_TOKEN; use --databricks-profile instead");
    }
    let mut vite = if prod {
        // Outside omnigent/server/static; per server, since the bundle inlines the workspace flag
        // and a build empties its dir under any preview still serving it.
        let out_dir = paths::default_pod_dir(&repo_root)?.join(format!(
            "web-ui-prod-{}",
            paths::short_hash(server.url.as_bytes())
        ));
        let out_dir = out_dir.to_string_lossy();
        eprintln!(
            "Building this checkout's production UI for {}",
            label(&server)
        );
        // The build must see OMNIGENT_URL too: it inlines the workspace flag.
        let built = pnpm(&web, &server, &["run", "build", "--outDir", &out_dir])
            .status()
            .context("building the UI with pnpm")?;
        if !built.success() {
            bail!("production UI build failed");
        }
        pnpm(
            &web,
            &server,
            &["run", "preview", "--outDir", &out_dir, "--open"],
        )
    } else {
        pnpm(&web, &server, &["run", "dev", "--open"])
    };
    eprintln!("Starting this checkout's UI against {}", label(&server));
    // exec so Ctrl-C and Vite's own keyboard shortcuts reach Vite directly.
    Err(vite.exec()).context("starting Vite with pnpm")
}

/// `pnpm <args>` in `web/` with the proxy env for `server`.
fn pnpm(web: &Path, server: &Server, args: &[&str]) -> Command {
    let mut command = Command::new("pnpm");
    command
        .args(args)
        .current_dir(web)
        .env("OMNIGENT_URL", &server.url)
        .env_remove("OMNIGENT_AUTH_TOKEN");
    match &server.databricks_profile {
        Some(profile) => command.env("OMNIGENT_DATABRICKS_PROFILE", profile),
        None => command.env_remove("OMNIGENT_DATABRICKS_PROFILE"),
    };
    command
}

/// Accept only what the dev proxy uses, so no credentials or typos land in the
/// history file: http(s), a host, an optional path and an optional `?o=<org-id>`.
fn check_url(url: &str) -> Result<()> {
    let rest = url
        .strip_prefix("https://")
        .or_else(|| url.strip_prefix("http://"));
    let valid = rest.is_some_and(|rest| {
        let (before_query, query) = rest.split_once('?').unwrap_or((rest, ""));
        let host = before_query.split('/').next().unwrap_or_default();
        let digits = |text: &str| !text.is_empty() && text.bytes().all(|b| b.is_ascii_digit());
        // `name[:port]`, where only a bracketed IPv6 name may itself contain ':'.
        let host_ok = match host.rsplit_once(':') {
            Some((name, port)) if digits(port) => !name.is_empty(),
            _ => {
                !host.is_empty()
                    && (!host.contains(':') || (host.starts_with('[') && host.ends_with(']')))
            }
        };
        let org_selector = query.strip_prefix("o=").is_some_and(digits);
        host_ok
            && !host.contains('@')
            && (query.is_empty() || org_selector)
            && !rest.contains('#')
            && !rest.chars().any(|c| c.is_whitespace() || c.is_control())
    });
    if !valid {
        // Not echoed: a rejected URL may hold credentials.
        bail!("server URL must look like http(s)://host[:port][/path][?o=<org-id>], without credentials");
    }
    Ok(())
}

/// Most recent first, each URL once; reusing a URL moves it to the front.
fn remember(recent: &mut Vec<Server>, server: Server) {
    recent.retain(|known| known.url != server.url);
    recent.insert(0, server);
    recent.truncate(MAX_RECENT);
}

fn load_recent(path: &Path) -> Vec<Server> {
    std::fs::read_to_string(path)
        .ok()
        .and_then(|text| serde_json::from_str(&text).ok())
        .unwrap_or_default()
}

fn save_recent(path: &Path, recent: &[Server]) -> Result<()> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)
            .with_context(|| format!("creating {}", parent.display()))?;
    }
    let text = serde_json::to_string_pretty(recent)?;
    // Write then rename, so an interrupted or concurrent run never leaves a partial file.
    let temporary = path.with_extension(format!("{}.tmp", std::process::id()));
    std::fs::write(&temporary, text).with_context(|| format!("writing {}", temporary.display()))?;
    std::fs::rename(&temporary, path).with_context(|| format!("replacing {}", path.display()))
}

fn label(server: &Server) -> String {
    match &server.databricks_profile {
        Some(profile) => format!("{}  (profile {profile})", server.url),
        None => server.url.clone(),
    }
}

/// URL of a recent server or of a new one typed in; `None` when cancelled.
fn pick(recent: &[Server]) -> Result<Option<String>> {
    if !std::io::stdin().is_terminal() || !std::io::stderr().is_terminal() {
        bail!("no terminal for the server picker; pass --server <URL>");
    }
    if recent.is_empty() {
        return prompt_url();
    }
    let mut rows: Vec<String> = recent.iter().map(label).collect();
    rows.push(NEW_URL_ROW.into());
    match select(&rows)? {
        Some(index) if index < recent.len() => Ok(Some(recent[index].url.clone())),
        Some(_) => prompt_url(),
        None => Ok(None),
    }
}

/// Line prompt for a new server; an empty answer cancels.
fn prompt_url() -> Result<Option<String>> {
    eprint!("Server URL (Databricks workspaces: keep ?o=<org-id>): ");
    std::io::stderr().flush()?;
    let mut line = String::new();
    std::io::stdin().read_line(&mut line)?;
    let url = line.trim();
    Ok((!url.is_empty()).then(|| url.to_string()))
}

/// Leaves raw mode on drop, so a panic or early return can't strand the terminal.
struct RawMode;

impl Drop for RawMode {
    fn drop(&mut self) {
        let _ = terminal::disable_raw_mode();
    }
}

/// Arrow-key list drawn inline on stderr; erased once a row is chosen.
fn select(rows: &[String]) -> Result<Option<usize>> {
    // Lines are truncated so none wraps, which keeps the MoveUp redraw exact.
    let width = match terminal::size() {
        Ok((columns, _)) if columns > 0 => usize::from(columns),
        _ => 80,
    } - 1;
    let lines = u16::try_from(rows.len() + 1).unwrap_or(u16::MAX);
    // stderr is unbuffered, so queued erases reach the terminal without a flush.
    let mut out = std::io::stderr();
    terminal::enable_raw_mode()?;
    let _raw_mode = RawMode;
    let mut selected = 0;
    loop {
        draw(&mut out, rows, selected, width)?;
        let key = loop {
            if let Event::Key(key) = event::read()? {
                if key.kind == KeyEventKind::Press {
                    break key;
                }
            }
        };
        queue!(out, MoveUp(lines), Clear(ClearType::FromCursorDown))?;
        match key.code {
            KeyCode::Up | KeyCode::Char('k') => {
                selected = selected.checked_sub(1).unwrap_or(rows.len() - 1)
            }
            KeyCode::Down | KeyCode::Char('j') | KeyCode::Tab => {
                selected = (selected + 1) % rows.len()
            }
            KeyCode::Enter => return Ok(Some(selected)),
            KeyCode::Esc | KeyCode::Char('q') => return Ok(None),
            KeyCode::Char('c') if key.modifiers.contains(KeyModifiers::CONTROL) => return Ok(None),
            _ => {}
        }
    }
}

fn draw(out: &mut impl Write, rows: &[String], selected: usize, width: usize) -> Result<()> {
    let fit = |text: String| text.chars().take(width).collect::<String>();
    write!(out, "{}\r\n", fit(PICKER_TITLE.into()))?;
    for (index, row) in rows.iter().enumerate() {
        let marker = if index == selected { '>' } else { ' ' };
        write!(out, "{}\r\n", fit(format!("{marker} {row}")))?;
    }
    Ok(out.flush()?)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn server(url: &str, profile: Option<&str>) -> Server {
        Server {
            url: url.into(),
            databricks_profile: profile.map(Into::into),
        }
    }

    #[test]
    fn remember_moves_a_reused_url_to_the_front_and_caps_the_list() {
        let mut recent: Vec<Server> = (0..MAX_RECENT)
            .map(|index| server(&format!("https://{index}.example.com"), None))
            .collect();

        remember(&mut recent, server("https://3.example.com", Some("dev")));
        assert_eq!(recent.len(), MAX_RECENT);
        assert_eq!(recent[0], server("https://3.example.com", Some("dev")));
        assert_eq!(
            recent
                .iter()
                .filter(|known| known.url == "https://3.example.com")
                .count(),
            1
        );

        remember(&mut recent, server("https://new.example.com", None));
        assert_eq!(recent.len(), MAX_RECENT);
        assert_eq!(recent[0].url, "https://new.example.com");
        assert_eq!(recent[MAX_RECENT - 1].url, "https://8.example.com");
    }

    #[test]
    fn recent_servers_round_trip_and_tolerate_missing_or_corrupt_files() {
        let dir = std::env::temp_dir().join(format!(
            "omnidev-recent-servers-test-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let path = dir.join("omnidev/recent-servers.json");
        assert!(load_recent(&path).is_empty());

        let recent = vec![
            server("https://a.example.com/?o=1", Some("dev")),
            server("http://localhost:6767", None),
        ];
        save_recent(&path, &recent).unwrap();
        assert_eq!(load_recent(&path), recent);

        std::fs::write(&path, "not json").unwrap();
        assert!(load_recent(&path).is_empty());
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn pnpm_gives_build_and_preview_the_server_env_without_a_static_token() {
        let command = pnpm(
            Path::new("/web"),
            &server("https://a.example.com/?o=1", None),
            &["run", "build"],
        );
        let envs: Vec<_> = command.get_envs().collect();
        assert!(envs.contains(&(
            "OMNIGENT_URL".as_ref(),
            Some("https://a.example.com/?o=1".as_ref())
        )));
        assert!(envs.contains(&("OMNIGENT_AUTH_TOKEN".as_ref(), None)));
        assert!(envs.contains(&("OMNIGENT_DATABRICKS_PROFILE".as_ref(), None)));
    }

    #[test]
    fn check_url_accepts_only_what_the_dev_proxy_uses() {
        for good in [
            "https://a.example.com/?o=1",
            "https://a.example.com/custom/mount",
            "http://localhost:6767",
            "http://[::1]:6767/",
            "http://[::1]",
        ] {
            assert!(check_url(good).is_ok(), "{good:?} should be accepted");
        }
        for bad in [
            "",
            "a.example.com",
            "ftp://a.example.com",
            "https://",
            "https:///path",
            "https://:443",
            "https://a.example.com:invalid",
            "https://a.example.com:",
            "https://a.example.com /",
            "https://user:secret@a.example.com",
            "https://a.example.com/?token=secret",
            "https://a.example.com/?o=1&token=secret",
            "https://a.example.com/?o=abc",
            "https://a.example.com/#token=secret",
        ] {
            assert!(check_url(bad).is_err(), "{bad:?} should be rejected");
        }
    }
}
