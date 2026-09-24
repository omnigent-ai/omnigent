//! Missing web tooling should fail before starting children or entering the TUI.

use std::fs;
use std::process::Command;

#[test]
fn missing_pnpm_and_corepack_fail_before_startup() {
    let repo = std::env::temp_dir().join(format!(
        "omnidev-web-prerequisites-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    for dir in [".git", "omnigent", "web", "bin"] {
        fs::create_dir_all(repo.join(dir)).unwrap();
    }
    let output = Command::new(env!("CARGO_BIN_EXE_omnidev"))
        .current_dir(&repo)
        .env("PATH", repo.join("bin"))
        .env("OMNIGENT_CONFIG_HOME", repo.join("config"))
        .args(["--pod-dir", repo.join("pod").to_str().unwrap()])
        .output()
        .unwrap();

    assert!(!output.status.success());
    let stderr = String::from_utf8(output.stderr).unwrap();
    assert!(
        stderr.contains("Neither `pnpm` nor `corepack` is on PATH"),
        "{stderr}"
    );
    assert!(stderr.contains("npm install -g pnpm"));
    assert!(stderr.contains("--no-vite"));
    assert!(output.stdout.is_empty(), "the TUI must not start");
    assert!(!repo.join("pod/logs/server.log").exists());
    fs::remove_dir_all(repo).unwrap();
}
