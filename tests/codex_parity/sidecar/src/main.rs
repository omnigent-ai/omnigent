//! Small process used by the Python parity tests.
//!
//! The tests want to run Omnigent -> real Codex CLI -> mock OpenAI Responses
//! API. Reusing Codex's Rust `core_test_support::responses` helpers keeps the
//! mock wire format aligned with upstream Codex tests, while this binary gives
//! pytest a simple JSONL protocol for starting the mock and reading captured
//! requests.

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
    // Each inner vector is one SSE response served for the next `/v1/responses`
    // request. The events are passed through Codex's own `responses::sse`
    // serializer instead of hand-building event-stream text in Python.
    responses: Vec<Vec<Value>>,
}

#[derive(Debug, Deserialize)]
struct Command {
    // Commands are newline-delimited JSON on stdin. Keeping the protocol tiny
    // avoids binding Python tests to WireMock or Rust internals.
    op: String,
    min: Option<usize>,
    timeout_ms: Option<u64>,
}

#[tokio::main]
async fn main() -> Result<()> {
    let config_path = config_path()?;
    let config = read_config(&config_path)?;

    // Start the same mock Responses server used by Codex's Rust tests, then
    // mount a sequence of SSE bodies so each model turn consumes one fixture.
    let server = responses::start_mock_server().await;
    let bodies = config
        .responses
        .into_iter()
        .map(responses::sse)
        .collect::<Vec<_>>();
    let response_mock = responses::mount_sse_sequence(&server, bodies).await;

    // Tell pytest where Codex should send its Responses API traffic. Codex is
    // configured with `base_url`, so the app-server posts to `/v1/responses`.
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

    // After startup, pytest drives the sidecar over stdin. The process stays
    // alive until shutdown so WireMock keeps serving and recording requests.
    let mut stdin = BufReader::new(tokio::io::stdin()).lines();
    while let Some(line) = stdin.next_line().await? {
        let command: Command = serde_json::from_str(&line).context("parse sidecar command")?;
        match command.op.as_str() {
            "requests" => {
                // Request capture is asynchronous relative to Codex app-server
                // output, so tests can ask us to wait until the expected number
                // of model requests has arrived before assertions run.
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

    // Dropping the MockServer lets WireMock run its expectation verification.
    // If a fixture response was never consumed, the process exits non-zero.
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
    // Return only stable, test-relevant fields. This keeps Python assertions
    // focused on Codex request shape instead of volatile HTTP details.
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
    // Header coverage matters for auth/thread-routing regressions, but dumping
    // every header makes tests noisy and version-sensitive.
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
