//! Git's global excludes file ranks below every repository rule and is
//! anchored at each repository's top level, wherever the helper runs from.

mod common;

use std::{collections::BTreeSet, path::Path, process::Command};

use common::{canonical, write};
use workspace_helper::{file_index::FileIndex, ignore_rules::IgnoreRules, walk::IgnoreMode};

/// Paths Git ignores, asking the repository that owns each path.
fn git_ignored(root: &Path, excludes: &Path, paths: &[(String, bool)]) -> BTreeSet<String> {
    paths
        .iter()
        .filter(|(path, is_dir)| {
            let absolute = root.join(path);
            let repository = absolute
                .parent()
                .unwrap()
                .ancestors()
                .find(|directory| directory.join(".git").exists())
                .unwrap();
            let within = absolute
                .strip_prefix(repository)
                .unwrap()
                .to_string_lossy()
                .into_owned();
            let within = if *is_dir {
                format!("{within}/")
            } else {
                within
            };
            Command::new("git")
                .args(["-c", &format!("core.excludesFile={}", excludes.display())])
                .args(["check-ignore", "--no-index", "-q", "--", &within])
                .current_dir(repository)
                .env("GIT_CONFIG_GLOBAL", "/dev/null")
                .env("GIT_CONFIG_NOSYSTEM", "1")
                .status()
                .unwrap()
                .success()
        })
        .map(|(path, _)| path.clone())
        .collect()
}

fn fixture_with_global_rules() -> (tempfile::TempDir, tempfile::NamedTempFile) {
    let fixture = common::ignore_fixture();
    let root = canonical(&fixture);
    for path in [
        "visible.txt",
        "src/visible.txt",
        "nested/visible.txt",
        "notes.bak",
        "src/old.bak",
        "wanted.keep",
    ] {
        write(&root, path, "");
    }
    // A repository rule re-includes what the global file excludes.
    let gitignore = std::fs::read_to_string(root.join(".gitignore")).unwrap();
    write(&root, ".gitignore", &(gitignore + "!wanted.keep\n"));
    let excludes = tempfile::NamedTempFile::new().unwrap();
    std::fs::write(excludes.path(), "/visible.txt\n*.bak\n*.keep\n").unwrap();
    (fixture, excludes)
}

#[test]
fn global_excludes_match_git_for_rules_and_the_index() {
    let (fixture, excludes) = fixture_with_global_rules();
    let root = canonical(&fixture);
    let rules = IgnoreRules::new(&root, Some(excludes.path().to_path_buf()));
    let everything: Vec<(String, bool)> =
        FileIndex::build(&root, IgnoreMode::None, &IgnoreRules::new(&root, None))
            .paths()
            .map(|entry| (entry.path.to_string(), entry.is_dir))
            .collect();
    let expected = git_ignored(&root, excludes.path(), &everything);
    // Anchored at each repository's top: the root's and nested's, not src's.
    assert!(expected.contains("visible.txt") && expected.contains("nested/visible.txt"));
    assert!(!expected.contains("src/visible.txt") && !expected.contains("wanted.keep"));

    let ours: BTreeSet<String> = everything
        .iter()
        .filter(|(path, is_dir)| rules.is_ignored(&root.join(path), *is_dir))
        .map(|(path, _)| path.clone())
        .collect();
    assert_eq!(ours, expected);

    let indexed: BTreeSet<String> = FileIndex::build(&root, IgnoreMode::Git, &rules)
        .paths()
        .map(|entry| entry.path.to_string())
        .collect();
    let not_ignored: BTreeSet<String> = everything
        .iter()
        .map(|(path, _)| path.clone())
        .filter(|path| {
            !path
                .split('/')
                .scan(String::new(), |prefix, part| {
                    if !prefix.is_empty() {
                        prefix.push('/');
                    }
                    prefix.push_str(part);
                    Some(prefix.clone())
                })
                .any(|ancestor| expected.contains(&ancestor))
        })
        .collect();
    assert_eq!(indexed, not_ignored);
}

#[test]
fn a_subdirectory_workspace_anchors_global_rules_at_the_repository_top() {
    let (fixture, excludes) = fixture_with_global_rules();
    let root = canonical(&fixture);
    let workspace = root.join("src");
    let rules = IgnoreRules::new(&workspace, Some(excludes.path().to_path_buf()));
    let indexed: Vec<String> = FileIndex::build(&workspace, IgnoreMode::Git, &rules)
        .paths()
        .map(|entry| entry.path.to_string())
        .collect();
    // "/visible.txt" names the repository's top-level file, not src/visible.txt.
    assert!(indexed.contains(&"visible.txt".to_string()), "{indexed:?}");
    assert!(!rules.is_ignored(&workspace.join("visible.txt"), false));
    assert!(!indexed.contains(&"old.bak".to_string()));
}
