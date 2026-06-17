use std::path::PathBuf;
use std::time::Duration;

use anyhow::Context;
use anyhow::Result;
use core_test_support::responses;
use core_test_support::responses::ResponseMock;
use serde::Deserialize;
use serde_json::Value;
use tokio::io::AsyncBufReadExt;
use tokio::io::AsyncWriteExt;
use tokio::io::BufReader;

#[derive(Debug, Deserialize)]
struct Config {
    responses: Vec<Vec<Value>>,
}

#[derive(Debug, Deserialize)]
struct Command {
    op: String,
    min: Option<usize>,
    timeout_ms: Option<u64>,
}

#[tokio::main]
async fn main() -> Result<()> {
    let config_path = config_path()?;
    let config = read_config(&config_path)?;
    let server = responses::start_mock_server().await;
    let bodies = config
        .responses
        .into_iter()
        .map(responses::sse)
        .collect::<Vec<_>>();
    let response_mock = responses::mount_sse_sequence(&server, bodies).await;

    let mut stdout = tokio::io::stdout();
    write_json_line(
        &mut stdout,
        &serde_json::json!({
            "type": "ready",
            "server_url": server.uri(),
            "base_url": format!("{}/v1", server.uri()),
        }),
    )
    .await?;

    let mut stdin = BufReader::new(tokio::io::stdin()).lines();
    while let Some(line) = stdin.next_line().await? {
        let command: Command = serde_json::from_str(&line).context("parse sidecar command")?;
        match command.op.as_str() {
            "requests" => {
                wait_for_requests(
                    &response_mock,
                    command.min.unwrap_or(0),
                    Duration::from_millis(command.timeout_ms.unwrap_or(0)),
                )
                .await;
                write_json_line(
                    &mut stdout,
                    &serde_json::json!({
                        "type": "requests",
                        "requests": captured_requests(&response_mock),
                    }),
                )
                .await?;
            }
            "shutdown" => {
                write_json_line(&mut stdout, &serde_json::json!({"type": "shutdown"})).await?;
                break;
            }
            other => {
                write_json_line(
                    &mut stdout,
                    &serde_json::json!({
                        "type": "error",
                        "message": format!("unknown op: {other}"),
                    }),
                )
                .await?;
            }
        }
    }

    drop(server);
    Ok(())
}

fn config_path() -> Result<PathBuf> {
    let mut args = std::env::args_os().skip(1);
    while let Some(arg) = args.next() {
        if arg == "--config" {
            let path = args.next().context("--config requires a path")?;
            return Ok(PathBuf::from(path));
        }
    }
    anyhow::bail!("usage: codex-parity-sidecar --config <path>");
}

fn read_config(path: &PathBuf) -> Result<Config> {
    let text = std::fs::read_to_string(path)
        .with_context(|| format!("read sidecar config {}", path.display()))?;
    serde_json::from_str(&text).context("parse sidecar config")
}

async fn wait_for_requests(mock: &ResponseMock, min: usize, timeout: Duration) {
    let deadline = tokio::time::Instant::now() + timeout;
    loop {
        if mock.requests().len() >= min {
            return;
        }
        if tokio::time::Instant::now() >= deadline {
            return;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
}

fn captured_requests(mock: &ResponseMock) -> Vec<Value> {
    mock.requests()
        .into_iter()
        .map(|request| {
            serde_json::json!({
                "path": request.path(),
                "headers": selected_headers(&request),
                "body": request.body_json(),
            })
        })
        .collect()
}

fn selected_headers(request: &responses::ResponsesRequest) -> Value {
    let names = [
        "authorization",
        "chatgpt-account-id",
        "content-encoding",
        "content-type",
        "user-agent",
        "x-codex-parent-thread-id",
        "x-codex-turn-metadata",
        "x-codex-window-id",
        "x-openai-subagent",
    ];
    let mut headers = serde_json::Map::new();
    for name in names {
        if let Some(value) = request.header(name) {
            headers.insert(name.to_string(), Value::String(value));
        }
    }
    Value::Object(headers)
}

async fn write_json_line(stdout: &mut tokio::io::Stdout, value: &Value) -> Result<()> {
    stdout
        .write_all(serde_json::to_string(value)?.as_bytes())
        .await?;
    stdout.write_all(b"\n").await?;
    stdout.flush().await?;
    Ok(())
}
