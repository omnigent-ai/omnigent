//! The helper agrees with `git` on what is ignored and with the runner's
//! ripgrep script on text search results.

mod common;

use std::{
    collections::BTreeSet,
    fs,
    path::{Path, PathBuf},
    process::Command,
};

use common::{canonical, git, write};
use workspace_helper::{
    file_index::FileIndex,
    ignore_rules::IgnoreRules,
    text_search::{TextQuery, search_all},
    walk::IgnoreMode,
};

/// Every path beneath `root` except Git's own directories.
fn all_paths(root: &Path) -> Vec<(String, bool)> {
    FileIndex::build(root, IgnoreMode::None, &IgnoreRules::new(root, None))
        .paths()
        .map(|entry| (entry.path.to_string(), entry.is_dir))
        .collect()
}

/// The nearest enclosing repository for a relative path, and the path within it.
fn owning_repository(root: &Path, path: &str) -> (PathBuf, String) {
    let mut directory = root.join(path).parent().unwrap().to_path_buf();
    while directory != root && !directory.join(".git").exists() {
        directory = directory.parent().unwrap().to_path_buf();
    }
    let within = root
        .join(path)
        .strip_prefix(&directory)
        .unwrap()
        .to_string_lossy()
        .into_owned();
    (directory, within)
}

fn git_says_ignored(root: &Path, path: &str, is_dir: bool) -> bool {
    let (repository, within) = owning_repository(root, path);
    let within = if is_dir { format!("{within}/") } else { within };
    Command::new("git")
        .args(["check-ignore", "--no-index", "-q", "--", &within])
        .current_dir(&repository)
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .status()
        .unwrap()
        .success()
}

#[test]
fn ignore_rules_agree_with_git_check_ignore() {
    let fixture = common::ignore_fixture();
    let root = canonical(&fixture);
    let rules = IgnoreRules::new(&root, None);
    let mut disagreements = Vec::new();
    for (path, is_dir) in all_paths(&root) {
        let ours = rules.is_ignored(&root.join(&path), is_dir);
        let theirs = git_says_ignored(&root, &path, is_dir);
        if ours != theirs {
            disagreements.push(format!("{path}: helper {ours}, git {theirs}"));
        }
    }
    assert!(disagreements.is_empty(), "{disagreements:#?}");
}

#[test]
fn the_file_index_holds_exactly_the_files_git_does_not_ignore() {
    let fixture = common::ignore_fixture();
    let root = canonical(&fixture);
    let index = FileIndex::build(&root, IgnoreMode::Git, &IgnoreRules::new(&root, None));
    let ours: BTreeSet<String> = index
        .paths()
        .filter(|entry| !entry.is_dir && !entry.path.starts_with("nested/"))
        .map(|entry| entry.path.to_string())
        .collect();
    let theirs: BTreeSet<String> = git(&root, &["ls-files", "--others", "--exclude-standard"])
        .lines()
        .filter(|path| !path.ends_with('/'))
        .map(str::to_string)
        .collect();
    assert_eq!(ours, theirs);
    let nested: BTreeSet<String> = index
        .paths()
        .filter(|entry| !entry.is_dir)
        .filter_map(|entry| entry.path.strip_prefix("nested/").map(str::to_string))
        .collect();
    let nested_git: BTreeSet<String> = git(
        &root.join("nested"),
        &["ls-files", "--others", "--exclude-standard"],
    )
    .lines()
    .map(str::to_string)
    .collect();
    assert_eq!(nested, nested_git);
}

#[test]
fn outside_a_repository_nothing_is_ignored() {
    let directory = tempfile::tempdir().unwrap();
    let root = canonical(&directory);
    write(&root, ".gitignore", "*.log\n");
    write(&root, "app.log", "");
    let rules = IgnoreRules::new(&root, None);
    assert!(!rules.is_ignored(&root.join("app.log"), false));
    let index = FileIndex::build(&root, IgnoreMode::Git, &rules);
    assert!(index.paths().any(|entry| entry.path.as_ref() == "app.log"));
}

#[test]
fn an_enclosing_repository_above_the_workspace_does_not_apply() {
    // A dotfiles repository in HOME that ignores everything must not hide a
    // separate repository beneath it.
    let directory = tempfile::tempdir().unwrap();
    let home = canonical(&directory);
    git(&home, &["init", "-q"]);
    write(&home, ".gitignore", "*\n");
    let project = home.join("project");
    write(&project, "src/main.rs", "");
    git(&project, &["init", "-q"]);
    let rules = IgnoreRules::new(&project, None);
    assert!(!rules.is_ignored(&project.join("src/main.rs"), false));
}

#[test]
fn a_workspace_inside_an_ignored_directory_is_ignored() {
    let fixture = common::ignore_fixture();
    let root = canonical(&fixture);
    let workspace = root.join("build");
    let rules = IgnoreRules::new(&workspace, None);
    assert!(rules.is_ignored(&workspace.join("output.bin"), false));
}

/// Runs the runner's own content-search script, as the runner runs it.
fn runner_search(
    root: &Path,
    query: &str,
    case_sensitive: bool,
    globs: &[&str],
) -> Option<serde_json::Value> {
    if Command::new("rg").arg("--version").output().is_err() {
        eprintln!("skipping runner parity: rg is not installed");
        return None;
    }
    let source = fs::read_to_string(
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../../omnigent/runner/content_search.py"),
    )
    .unwrap();
    let start = source.find("_SEARCH_SCRIPT = r\"\"\"").unwrap() + "_SEARCH_SCRIPT = r\"\"\"".len();
    let end = start + source[start..].find("\"\"\"").unwrap();
    let parameters = format!(
        "query = {query:?}\ncase_sensitive = {}\nregex = False\nlimit = 500\nglob_args = {:?}\n",
        if case_sensitive { "True" } else { "False" },
        globs
    );
    let output = Command::new("python3")
        .args(["-I", "-c", &(parameters + &source[start..end])])
        .current_dir(root)
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("RIPGREP_CONFIG_PATH", "/dev/null")
        .output()
        .unwrap();
    Some(serde_json::from_slice(&output.stdout).unwrap())
}

#[test]
fn text_search_matches_the_runners_ripgrep_search() {
    let fixture = common::ignore_fixture();
    let root = canonical(&fixture);
    let long_line = format!(
        "{}Needle in a 😀 long line{}\n",
        "x".repeat(150),
        "y".repeat(600)
    );
    write(&root, "src/long.rs", &long_line);
    write(
        &root,
        "src/lib.rs",
        "fn needle() {}\nlet NEEDLE = \"é needle\";\n",
    );
    write(&root, "docs/notes.md", "needle\n");
    write(&root, "app.log", "needle in an ignored file\n");
    write(&root, ".ignore", "docs/\n");
    // ripgrep's own ignore file, nested and at the root.
    write(&root, ".rgignore", "rg-hidden.txt\n");
    write(&root, "rg-hidden.txt", "needle hidden from rg\n");
    write(&root, "src/.rgignore", "!rg-hidden.txt\n");
    write(&root, "src/rg-hidden.txt", "needle re-included\n");
    write(&root, "binary.bin", "needle\0binary");

    for (case_sensitive, include, globs) in [
        (false, "", vec![]),
        (true, "", vec![]),
        (false, "*.rs", vec!["--glob=*.rs"]),
    ] {
        let Some(runner) = runner_search(&root, "needle", case_sensitive, &globs) else {
            return;
        };
        let query = TextQuery {
            query: "needle".into(),
            case_sensitive,
            include: include.into(),
            limit: 500,
            ..TextQuery::default()
        };
        let (matches, summary) = search_all(&root, &query, None).unwrap();
        let ours: Vec<serde_json::Value> = matches
            .iter()
            .map(|found| {
                serde_json::json!({
                    "path": found.path, "line": found.line, "column": found.column,
                    "text": found.text, "match_start": found.match_start, "match_end": found.match_end,
                })
            })
            .collect();
        let mut theirs: Vec<serde_json::Value> = runner["matches"].as_array().unwrap().clone();
        theirs.sort_by_key(|found| {
            (
                found["path"].as_str().unwrap().to_string(),
                found["line"].as_u64().unwrap(),
            )
        });
        assert_eq!(
            ours, theirs,
            "case_sensitive={case_sensitive} include={include:?}"
        );
        assert_eq!(summary.truncated, runner["truncated"].as_bool().unwrap());
        assert!(!ours.is_empty());
    }
}
