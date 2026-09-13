//! Open the omnidev UI in the platform browser without going through a shell.

use std::io::ErrorKind;
use std::process::Stdio;

use anyhow::{bail, Context, Result};
use tokio::process::Command;

struct OpenCommand {
    program: &'static str,
    args: Vec<String>,
}

/// Open `url` with the platform's standard browser launcher.
pub async fn open(url: &str) -> Result<()> {
    for candidate in candidates(url) {
        let status = Command::new(candidate.program)
            .args(&candidate.args)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .await;
        match status {
            Ok(status) if status.success() => return Ok(()),
            Ok(status) => bail!("{} exited with {status}", candidate.program),
            Err(error) if error.kind() == ErrorKind::NotFound => continue,
            Err(error) => {
                return Err(error)
                    .with_context(|| format!("starting browser opener {}", candidate.program));
            }
        }
    }
    bail!("no supported browser opener was found")
}

#[cfg(target_os = "macos")]
fn candidates(url: &str) -> Vec<OpenCommand> {
    vec![OpenCommand {
        program: "/usr/bin/open",
        args: vec![url.into()],
    }]
}

#[cfg(target_os = "linux")]
fn candidates(url: &str) -> Vec<OpenCommand> {
    vec![
        OpenCommand {
            program: "xdg-open",
            args: vec![url.into()],
        },
        OpenCommand {
            program: "gio",
            args: vec!["open".into(), url.into()],
        },
    ]
}

#[cfg(not(any(target_os = "macos", target_os = "linux")))]
fn candidates(_url: &str) -> Vec<OpenCommand> {
    Vec::new()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    #[cfg(target_os = "macos")]
    fn macos_uses_open() {
        let commands = candidates("http://localhost:5173");
        assert_eq!(commands.len(), 1);
        assert_eq!(commands[0].program, "/usr/bin/open");
        assert_eq!(commands[0].args, ["http://localhost:5173"]);
    }

    #[test]
    #[cfg(target_os = "linux")]
    fn linux_uses_desktop_openers() {
        let commands = candidates("http://localhost:5173");
        assert_eq!(commands.len(), 2);
        assert_eq!(commands[0].program, "xdg-open");
        assert_eq!(commands[0].args, ["http://localhost:5173"]);
        assert_eq!(commands[1].program, "gio");
        assert_eq!(commands[1].args, ["open", "http://localhost:5173"]);
    }
}
