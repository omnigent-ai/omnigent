//! Working-copy comparisons over the real transport, on temporary repositories.

mod common;

use std::{
    collections::BTreeMap,
    fs,
    os::unix::fs::{MetadataExt, symlink},
    path::{Path, PathBuf},
};

use common::{Helper, collect, git, start, write};
use tonic::Code;
use walkdir_lite::files_under;
use workspace_helper::proto::{
    ChangeStatus, ChangedFile, ChangedFileProblem, Comparison, ComparisonKind, ComparisonProblem,
    GitEntryKind, ListWorkingCopyChangesRequest, ListWorkingCopyChangesResponse,
    ReadBaseFileRequest, ReadLinkTextRequest,
};

/// A small recursive file walk, so the test needs no extra dependency.
mod walkdir_lite {
    use std::path::{Path, PathBuf};

    pub fn files_under(root: &Path) -> Vec<PathBuf> {
        let mut found = Vec::new();
        let mut pending = vec![root.to_path_buf()];
        while let Some(directory) = pending.pop() {
            let Ok(entries) = std::fs::read_dir(&directory) else {
                continue;
            };
            for entry in entries.flatten() {
                let path = entry.path();
                if entry.file_type().is_ok_and(|kind| kind.is_dir()) {
                    pending.push(path);
                } else {
                    found.push(path);
                }
            }
        }
        found.sort();
        found
    }
}

fn commit(root: &Path, message: &str) {
    git(root, &["add", "-A"]);
    git(
        root,
        &[
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--quiet",
            "--allow-empty",
            "--message",
            message,
        ],
    );
}

/// A remote as a clone configures it, so branch tracking maps to its refs.
fn add_remote(root: &Path, name: &str) {
    git(
        root,
        &[
            "config",
            &format!("remote.{name}.url"),
            &format!("https://example.invalid/{name}.git"),
        ],
    );
    git(
        root,
        &[
            "config",
            &format!("remote.{name}.fetch"),
            &format!("+refs/heads/*:refs/remotes/{name}/*"),
        ],
    );
}

fn head(root: &Path) -> String {
    git(root, &["rev-parse", "HEAD"]).trim().to_string()
}

fn repository() -> (tempfile::TempDir, PathBuf) {
    let directory = tempfile::tempdir().unwrap();
    let root = fs::canonicalize(directory.path()).unwrap();
    git(&root, &["init", "-q", "-b", "main"]);
    (directory, root)
}

/// Everything in `.git` that a read must leave alone: each file's bytes,
/// size, and modification time.
fn repository_state(git_directory: &Path) -> BTreeMap<PathBuf, (u64, i64, i64, Vec<u8>)> {
    files_under(git_directory)
        .into_iter()
        .map(|path| {
            let metadata = fs::symlink_metadata(&path).unwrap();
            let bytes = fs::read(&path).unwrap_or_default();
            (
                path,
                (
                    metadata.size(),
                    metadata.mtime(),
                    metadata.mtime_nsec(),
                    bytes,
                ),
            )
        })
        .collect()
}

async fn list(
    helper: &mut Helper,
    workspace: &Path,
    kind: ComparisonKind,
) -> ListWorkingCopyChangesResponse {
    helper.open(workspace).await;
    helper
        .client
        .list_working_copy_changes(ListWorkingCopyChangesRequest {
            comparison: Some(Comparison {
                kind: kind as i32,
                ..Comparison::default()
            }),
        })
        .await
        .unwrap()
        .into_inner()
}

fn file<'a>(changes: &'a ListWorkingCopyChangesResponse, path: &str) -> &'a ChangedFile {
    changes
        .files
        .iter()
        .find(|file| file.path == path)
        .unwrap_or_else(|| panic!("{path} not listed in {:?}", paths(changes)))
}

fn paths(changes: &ListWorkingCopyChangesResponse) -> Vec<&str> {
    changes
        .files
        .iter()
        .map(|file| file.path.as_str())
        .collect()
}

fn problem(changes: &ListWorkingCopyChangesResponse) -> ComparisonProblem {
    if let Some(unavailable) = changes
        .base
        .as_ref()
        .and_then(|base| base.unavailable.as_ref())
    {
        eprintln!("comparison unavailable: {unavailable:?}");
    }
    changes
        .base
        .as_ref()
        .and_then(|base| base.unavailable.as_ref())
        .map(|unavailable| unavailable.problem())
        .unwrap_or(ComparisonProblem::Unspecified)
}

/// main at c1; `feature` two commits ahead, tracking `upstream/master` (= c1).
/// `origin/HEAD` points at a fork's branch that is further behind.
fn branch_fixture() -> (tempfile::TempDir, PathBuf, String) {
    let (directory, root) = repository();
    write(&root, "kept.txt", "kept\n");
    write(&root, "edited.txt", "one\n");
    commit(&root, "c0");
    let fork_commit = head(&root);
    write(&root, "base.txt", "base\n");
    commit(&root, "c1");
    let branch_point = head(&root);
    add_remote(&root, "upstream");
    add_remote(&root, "origin");
    git(
        &root,
        &["update-ref", "refs/remotes/upstream/master", &branch_point],
    );
    git(
        &root,
        &["update-ref", "refs/remotes/origin/master", &fork_commit],
    );
    git(
        &root,
        &[
            "symbolic-ref",
            "refs/remotes/origin/HEAD",
            "refs/remotes/origin/master",
        ],
    );
    git(&root, &["checkout", "-q", "-b", "feature"]);
    git(&root, &["config", "branch.feature.remote", "upstream"]);
    git(
        &root,
        &["config", "branch.feature.merge", "refs/heads/master"],
    );
    write(&root, "committed.txt", "on the branch\n");
    commit(&root, "c2");
    write(&root, "edited.txt", "one\ntwo\n");
    commit(&root, "c3");
    (directory, root, branch_point)
}

#[tokio::test(flavor = "multi_thread")]
async fn last_commit_lists_modified_deleted_and_untracked_files() {
    let (_directory, root) = repository();
    write(&root, "a.txt", "one\ntwo\n");
    write(&root, "gone.txt", "bye\n");
    commit(&root, "c1");
    write(&root, "a.txt", "one\nTWO\nthree\n");
    fs::remove_file(root.join("gone.txt")).unwrap();
    write(&root, "new.txt", "hello\n");

    let mut helper = start().await;
    let changes = list(&mut helper, &root, ComparisonKind::LastCommit).await;
    assert_eq!(problem(&changes), ComparisonProblem::Unspecified);
    assert_eq!(paths(&changes), ["a.txt", "gone.txt", "new.txt"]);
    let base = changes.base.as_ref().unwrap();
    assert_eq!(base.base, head(&root));
    assert_eq!(base.base.len(), 40);

    let modified = file(&changes, "a.txt");
    assert_eq!(modified.status(), ChangeStatus::Modified);
    assert_eq!(
        (modified.lines_added, modified.lines_removed),
        (Some(2), Some(1))
    );
    assert_eq!(modified.base_object.len(), 40, "full object names");
    assert_eq!(modified.base_size, Some(8));
    assert_eq!(file(&changes, "gone.txt").status(), ChangeStatus::Deleted);
    let added = file(&changes, "new.txt");
    assert_eq!(added.status(), ChangeStatus::Added);
    assert!(added.base_object.is_empty());
}

#[tokio::test(flavor = "multi_thread")]
async fn branch_point_uses_the_tracked_remote_and_includes_committed_files() {
    let (_directory, root, branch_point) = branch_fixture();
    write(&root, "untracked.txt", "new\n");
    let mut helper = start().await;

    let changes = list(&mut helper, &root, ComparisonKind::BranchPoint).await;
    let base = changes.base.as_ref().unwrap();
    assert_eq!(problem(&changes), ComparisonProblem::Unspecified);
    // Not the fork that origin/HEAD names.
    assert_eq!(base.reference, "upstream/master");
    assert_eq!(base.base, branch_point);
    assert_eq!(
        paths(&changes),
        ["committed.txt", "edited.txt", "untracked.txt"]
    );

    // Against the last commit, the committed files aren't changes.
    let last = list(&mut helper, &root, ComparisonKind::LastCommit).await;
    assert_eq!(paths(&last), ["untracked.txt"]);
}

#[tokio::test(flavor = "multi_thread")]
async fn branch_point_falls_back_to_the_remote_default_branch() {
    let (_directory, root, _) = branch_fixture();
    // The tracked remote names its default branch: that wins over the upstream.
    let main = git(&root, &["rev-parse", "main"]).trim().to_string();
    git(&root, &["update-ref", "refs/remotes/upstream/main", &main]);
    git(
        &root,
        &[
            "symbolic-ref",
            "refs/remotes/upstream/HEAD",
            "refs/remotes/upstream/main",
        ],
    );
    let mut helper = start().await;
    let changes = list(&mut helper, &root, ComparisonKind::BranchPoint).await;
    assert_eq!(changes.base.as_ref().unwrap().reference, "upstream/main");

    // A branch pushed under its own name, with no remote default: origin/HEAD.
    let (_other, root) = repository();
    write(&root, "a.txt", "a\n");
    commit(&root, "c1");
    let first = head(&root);
    add_remote(&root, "origin");
    git(&root, &["update-ref", "refs/remotes/origin/main", &first]);
    git(
        &root,
        &[
            "symbolic-ref",
            "refs/remotes/origin/HEAD",
            "refs/remotes/origin/main",
        ],
    );
    git(&root, &["checkout", "-q", "-b", "topic"]);
    write(&root, "b.txt", "b\n");
    commit(&root, "c2");
    git(
        &root,
        &["update-ref", "refs/remotes/origin/topic", &head(&root)],
    );
    git(&root, &["config", "branch.topic.remote", "origin"]);
    git(&root, &["config", "branch.topic.merge", "refs/heads/topic"]);
    let changes = list(&mut helper, &root, ComparisonKind::BranchPoint).await;
    let base = changes.base.as_ref().unwrap();
    assert_eq!(base.reference, "origin/main");
    assert_eq!(base.base, first);
    assert_eq!(paths(&changes), ["b.txt"]);
}

#[tokio::test(flavor = "multi_thread")]
async fn branch_point_reports_why_it_is_unavailable() {
    let mut helper = start().await;

    let (_empty, root) = repository();
    let changes = list(&mut helper, &root, ComparisonKind::BranchPoint).await;
    assert_eq!(problem(&changes), ComparisonProblem::NoCommits);

    let (_lonely, root) = repository();
    write(&root, "a.txt", "a\n");
    commit(&root, "c1");
    let changes = list(&mut helper, &root, ComparisonKind::BranchPoint).await;
    assert_eq!(problem(&changes), ComparisonProblem::NoDefaultBranch);
    // HEAD still works.
    let changes = list(&mut helper, &root, ComparisonKind::LastCommit).await;
    assert_eq!(problem(&changes), ComparisonProblem::Unspecified);

    // A default branch with unrelated history.
    git(&root, &["checkout", "-q", "--orphan", "other"]);
    git(&root, &["rm", "-rq", "--cached", "."]);
    write(&root, "b.txt", "b\n");
    commit(&root, "unrelated");
    git(
        &root,
        &["update-ref", "refs/remotes/origin/main", &head(&root)],
    );
    git(
        &root,
        &[
            "symbolic-ref",
            "refs/remotes/origin/HEAD",
            "refs/remotes/origin/main",
        ],
    );
    git(&root, &["checkout", "-q", "main"]);
    let changes = list(&mut helper, &root, ComparisonKind::BranchPoint).await;
    assert_eq!(problem(&changes), ComparisonProblem::NoBranchPoint);
    let unavailable = changes.base.unwrap().unavailable.unwrap();
    assert_eq!(unavailable.reference, "origin/main");
    assert!(!unavailable.shallow);

    let outside = tempfile::tempdir().unwrap();
    let changes = list(
        &mut helper,
        &fs::canonicalize(outside.path()).unwrap(),
        ComparisonKind::LastCommit,
    )
    .await;
    assert_eq!(problem(&changes), ComparisonProblem::NotARepository);
}

#[tokio::test(flavor = "multi_thread")]
async fn a_workspace_below_the_repository_top_sees_only_its_subtree() {
    let (_directory, root) = repository();
    let workspace = root.join("w*[x]?");
    write(&workspace, "sub/f.txt", "a\n");
    write(&root, "other/g.txt", "b\n");
    write(&workspace, "top.txt", "c\n");
    commit(&root, "c1");
    write(&workspace, "sub/f.txt", "a\nb\n");
    write(&root, "other/g.txt", "changed\n");
    write(&workspace, "new.txt", "n\n");
    write(&root, "other/new.txt", "n\n");

    let mut helper = start().await;
    let changes = list(&mut helper, &workspace, ComparisonKind::LastCommit).await;
    assert_eq!(problem(&changes), ComparisonProblem::Unspecified);
    assert_eq!(paths(&changes), ["new.txt", "sub/f.txt"]);
    assert_eq!(file(&changes, "sub/f.txt").lines_added, Some(1));

    // A base read names the file relative to the workspace, so the sibling is unreachable.
    helper.open(&workspace).await;
    let entry = file(&changes, "sub/f.txt");
    let read = |path: &str, object: &str| ReadBaseFileRequest {
        commit: changes.base.as_ref().unwrap().base.clone(),
        path: path.into(),
        object: object.into(),
        maximum_bytes: 0,
    };
    let chunks = collect(
        helper
            .client
            .read_base_file(read("sub/f.txt", &entry.base_object))
            .await
            .unwrap()
            .into_inner(),
    )
    .await
    .unwrap();
    let bytes: Vec<u8> = chunks.iter().flat_map(|chunk| chunk.data.clone()).collect();
    assert_eq!(bytes, b"a\n");
    let escape = helper
        .client
        .read_base_file(read("../other/g.txt", &entry.base_object))
        .await
        .unwrap_err();
    assert_eq!(escape.code(), Code::InvalidArgument);
    let wrong_object = helper
        .client
        .read_base_file(read("top.txt", &entry.base_object))
        .await
        .unwrap_err();
    assert_eq!(wrong_object.code(), Code::FailedPrecondition);
}

#[tokio::test(flavor = "multi_thread")]
async fn base_reads_stop_at_the_allowance_without_returning_partial_text() {
    let (_directory, root) = repository();
    write(&root, "big.txt", &"x".repeat(100_000));
    commit(&root, "c1");
    write(&root, "big.txt", "small\n");
    let mut helper = start().await;
    let changes = list(&mut helper, &root, ComparisonKind::LastCommit).await;
    let entry = file(&changes, "big.txt");
    helper.open(&root).await;
    let request = |maximum_bytes| ReadBaseFileRequest {
        commit: changes.base.as_ref().unwrap().base.clone(),
        path: "big.txt".into(),
        object: entry.base_object.clone(),
        maximum_bytes,
    };
    for maximum in [1_000, 99_999] {
        let chunks = collect(
            helper
                .client
                .read_base_file(request(maximum))
                .await
                .unwrap()
                .into_inner(),
        )
        .await
        .unwrap();
        // Too large to send: metadata alone, marked truncated.
        assert_eq!(chunks.len(), 1);
        let metadata = chunks[0].metadata.clone().unwrap();
        assert!(metadata.truncated && metadata.size == 0, "{maximum}");
    }
    let chunks = collect(
        helper
            .client
            .read_base_file(request(100_000))
            .await
            .unwrap()
            .into_inner(),
    )
    .await
    .unwrap();
    assert!(!chunks[0].metadata.clone().unwrap().truncated);
    assert_eq!(
        chunks.iter().map(|chunk| chunk.data.len()).sum::<usize>(),
        100_000
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn comparisons_never_change_the_repository_or_run_configured_helpers() {
    let (_directory, root, _) = branch_fixture();
    let markers = tempfile::tempdir().unwrap();
    let marker = |name: &str| markers.path().join(name);
    let script = |name: &str, tail: &str| {
        let path = markers.path().join(format!("{name}.sh"));
        fs::write(
            &path,
            format!("#!/bin/sh\ntouch '{}'\n{tail}\n", marker(name).display()),
        )
        .unwrap();
        fs::set_permissions(&path, std::os::unix::fs::PermissionsExt::from_mode(0o755)).unwrap();
        path.display().to_string()
    };
    let clean = script("clean", "cat");
    let process = script("process", "exit 1");
    let textconv = script("textconv", "cat \"$1\"");
    let external = script("external", "exit 0");
    let fsmonitor = script("fsmonitor", "exit 1");
    let credential = script("credential", "exit 0");
    let hook = script("hook", "exit 0");
    // Driver names are case-sensitive, so the overrides must keep this spelling.
    write(
        &root,
        ".gitattributes",
        "*.dat filter=StrictCase diff=shown\n",
    );
    write(&root, "data.dat", "one\n");
    commit(&root, "attributes");
    for (key, value) in [
        ("filter.StrictCase.clean", clean.as_str()),
        ("filter.StrictCase.process", process.as_str()),
        ("filter.StrictCase.required", "true"),
        ("diff.shown.textconv", textconv.as_str()),
        ("diff.external", external.as_str()),
        ("core.fsmonitor", fsmonitor.as_str()),
        ("credential.helper", credential.as_str()),
        ("core.hooksPath", markers.path().to_str().unwrap()),
    ] {
        git(&root, &["config", key, value]);
    }
    fs::copy(&hook, markers.path().join("post-checkout")).unwrap();
    // Changed after the index was written, so Git must look at the content.
    std::thread::sleep(std::time::Duration::from_millis(1100));
    write(&root, "data.dat", "one\ntwo\n");
    write(&root, "edited.txt", "one\ntwo\nthree\n");
    let before = repository_state(&root.join(".git"));

    let mut helper = start().await;
    for kind in [ComparisonKind::LastCommit, ComparisonKind::BranchPoint] {
        let changes = list(&mut helper, &root, kind).await;
        assert_eq!(
            problem(&changes),
            ComparisonProblem::Unspecified,
            "{kind:?}"
        );
        assert_eq!(
            file(&changes, "data.dat").problem(),
            ChangedFileProblem::Filtered
        );
        let edited = file(&changes, "edited.txt");
        helper.open(&root).await;
        collect(
            helper
                .client
                .read_base_file(ReadBaseFileRequest {
                    commit: changes.base.as_ref().unwrap().base.clone(),
                    path: "edited.txt".into(),
                    object: edited.base_object.clone(),
                    maximum_bytes: 0,
                })
                .await
                .unwrap()
                .into_inner(),
        )
        .await
        .unwrap();
    }

    for name in [
        "clean",
        "process",
        "textconv",
        "external",
        "fsmonitor",
        "credential",
        "hook",
    ] {
        assert!(!marker(name).exists(), "{name} ran");
    }
    assert_eq!(
        before,
        repository_state(&root.join(".git")),
        "the repository changed"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn a_partial_clone_reports_missing_objects_instead_of_fetching() {
    let (_origin_directory, origin) = repository();
    git(&origin, &["config", "uploadpack.allowFilter", "true"]);
    write(&origin, "a.txt", "version one\n");
    commit(&origin, "c1");
    let first = head(&origin);
    write(&origin, "a.txt", "version two\n");
    commit(&origin, "c2");
    let clones = tempfile::tempdir().unwrap();
    let clone = fs::canonicalize(clones.path()).unwrap().join("clone");
    git(
        clones.path(),
        &[
            "clone",
            "-q",
            "--filter=blob:none",
            &format!("file://{}", origin.display()),
            clone.to_str().unwrap(),
        ],
    );
    // The first commit's blob was never downloaded; compare with it.
    add_remote(&clone, "upstream");
    git(
        &clone,
        &["update-ref", "refs/remotes/upstream/release", &first],
    );
    git(&clone, &["config", "branch.main.remote", "upstream"]);
    git(
        &clone,
        &["config", "branch.main.merge", "refs/heads/release"],
    );
    // Contacting the promisor remote would now fail loudly or hang.
    let moved = origin.with_extension("moved");
    fs::rename(&origin, &moved).unwrap();
    let before = repository_state(&clone.join(".git"));

    let mut helper = start().await;
    let changes = list(&mut helper, &clone, ComparisonKind::BranchPoint).await;
    let base = changes.base.as_ref().unwrap();
    if problem(&changes) == ComparisonProblem::PartialCloneNeedsNewerGit {
        return; // An older Git on this machine is refused, which is also correct.
    }
    assert_eq!(problem(&changes), ComparisonProblem::Unspecified);
    assert_eq!(base.base, first);
    let entry = file(&changes, "a.txt");
    assert_eq!(entry.problem(), ChangedFileProblem::MissingObject);
    assert_eq!(entry.lines_added, None);

    helper.open(&clone).await;
    let missing = helper
        .client
        .read_base_file(ReadBaseFileRequest {
            commit: first,
            path: "a.txt".into(),
            object: entry.base_object.clone(),
            maximum_bytes: 0,
        })
        .await;
    let error = match missing {
        Ok(stream) => collect(stream.into_inner()).await.unwrap_err(),
        Err(error) => error,
    };
    assert_eq!(error.code(), Code::NotFound);
    assert_eq!(
        before,
        repository_state(&clone.join(".git")),
        "objects were fetched"
    );
    fs::rename(&moved, &origin).unwrap();
}

#[tokio::test(flavor = "multi_thread")]
async fn symlinks_are_compared_by_their_own_text() {
    let (_directory, root) = repository();
    let outside = tempfile::tempdir().unwrap();
    fs::write(outside.path().join("secret"), "secret contents\n").unwrap();
    write(&root, "regular.txt", "was a file\n");
    write(&root, "target.txt", "target\n");
    symlink("target.txt", root.join("link")).unwrap();
    commit(&root, "c1");
    fs::remove_file(root.join("regular.txt")).unwrap();
    symlink("target.txt", root.join("regular.txt")).unwrap();
    fs::remove_file(root.join("link")).unwrap();
    symlink("missing.txt", root.join("link")).unwrap();
    symlink(outside.path().join("secret"), root.join("escape")).unwrap();

    let mut helper = start().await;
    let changes = list(&mut helper, &root, ComparisonKind::LastCommit).await;
    let changed = file(&changes, "regular.txt");
    assert_eq!(changed.status(), ChangeStatus::TypeChanged);
    assert_eq!(changed.base_kind(), GitEntryKind::File);
    assert_eq!(changed.working_kind(), GitEntryKind::Symlink);
    assert_eq!(file(&changes, "link").working_kind(), GitEntryKind::Symlink);
    assert_eq!(
        file(&changes, "escape").working_kind(),
        GitEntryKind::Symlink
    );

    helper.open(&root).await;
    let link_text = |path: &str| ReadLinkTextRequest { path: path.into() };
    let broken = helper
        .client
        .read_link_text(link_text("link"))
        .await
        .unwrap()
        .into_inner();
    assert_eq!(broken.target, b"missing.txt");
    // The link's text, never what it points to.
    let escape = helper
        .client
        .read_link_text(link_text("escape"))
        .await
        .unwrap()
        .into_inner();
    assert_eq!(
        escape.target,
        outside.path().join("secret").to_str().unwrap().as_bytes()
    );
    let not_link = helper
        .client
        .read_link_text(link_text("target.txt"))
        .await
        .unwrap_err();
    assert_eq!(not_link.code(), Code::FailedPrecondition);
    // A parent directory that leaves the workspace is refused.
    symlink(outside.path(), root.join("away")).unwrap();
    let away = helper
        .client
        .read_link_text(link_text("away/secret"))
        .await
        .unwrap_err();
    assert_eq!(away.code(), Code::PermissionDenied);
}

#[tokio::test(flavor = "multi_thread")]
async fn a_conflicted_file_is_listed_once_as_conflicted() {
    let (_directory, root) = repository();
    write(&root, "c.txt", "base\n");
    commit(&root, "c1");
    git(&root, &["checkout", "-q", "-b", "left"]);
    write(&root, "c.txt", "left\n");
    commit(&root, "left");
    git(&root, &["checkout", "-q", "main"]);
    write(&root, "c.txt", "right\n");
    commit(&root, "right");
    let output = std::process::Command::new("git")
        .args([
            "-c",
            "user.name=T",
            "-c",
            "user.email=t@e",
            "merge",
            "-q",
            "left",
        ])
        .current_dir(&root)
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .output()
        .unwrap();
    assert!(!output.status.success(), "the merge conflicts");

    let mut helper = start().await;
    let changes = list(&mut helper, &root, ComparisonKind::LastCommit).await;
    assert_eq!(problem(&changes), ComparisonProblem::Unspecified);
    let conflicted: Vec<_> = changes
        .files
        .iter()
        .filter(|file| file.path == "c.txt")
        .collect();
    assert_eq!(conflicted.len(), 1, "one entry per conflicted path");
    assert_eq!(conflicted[0].status(), ChangeStatus::Conflicted);
}

#[tokio::test(flavor = "multi_thread")]
async fn untracked_files_follow_gits_ignore_rules_as_they_change() {
    let (_directory, root) = repository();
    write(&root, ".gitignore", "*.log\n!keep.log\nbuild/\n");
    write(&root, "tracked.log", "tracked\n");
    git(&root, &["add", "-f", "tracked.log"]);
    commit(&root, "c1");
    write(&root, ".git/info/exclude", "local.tmp\n");
    write(&root, "tracked.log", "tracked and edited\n");
    write(&root, "debug.log", "ignored\n");
    write(&root, "keep.log", "unignored\n");
    write(&root, "local.tmp", "excluded for this clone\n");
    write(&root, "build/out.txt", "ignored directory\n");
    write(&root, "new.txt", "new\n");
    symlink("build", root.join("build-link")).unwrap();
    symlink("nowhere", root.join("dangling")).unwrap();

    let mut helper = start().await;
    let changes = list(&mut helper, &root, ComparisonKind::LastCommit).await;
    assert_eq!(problem(&changes), ComparisonProblem::Unspecified);
    assert_eq!(
        paths(&changes),
        [
            "build-link",
            "dangling",
            "keep.log",
            "new.txt",
            "tracked.log"
        ]
    );
    // An ignored file that is tracked still shows its edits.
    assert_eq!(
        file(&changes, "tracked.log").status(),
        ChangeStatus::Modified
    );
    // A link to a directory is an untracked link, not a directory.
    assert_eq!(
        file(&changes, "build-link").working_kind(),
        GitEntryKind::Symlink
    );
    assert_eq!(
        file(&changes, "dangling").working_kind(),
        GitEntryKind::Symlink
    );

    // Each listing reads the rules afresh.
    write(&root, ".gitignore", "*.log\nbuild/\n");
    write(&root, ".git/info/exclude", "local.tmp\nnew.txt\n");
    let changes = list(&mut helper, &root, ComparisonKind::LastCommit).await;
    assert_eq!(
        paths(&changes),
        [".gitignore", "build-link", "dangling", "tracked.log"]
    );
    assert_eq!(
        file(&changes, ".gitignore").status(),
        ChangeStatus::Modified
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn a_nested_repository_makes_the_comparison_unavailable_but_a_submodule_does_not() {
    let (_directory, root) = repository();
    write(&root, "a.txt", "a\n");
    let submodule = root.join("sub");
    fs::create_dir(&submodule).unwrap();
    git(&submodule, &["init", "-q", "-b", "main"]);
    write(&submodule, "inner.txt", "inner\n");
    commit(&submodule, "inner c1");
    commit(&root, "c1");
    write(&submodule, "inner.txt", "inner edited\n");
    commit(&submodule, "inner c2");

    let mut helper = start().await;
    let changes = list(&mut helper, &root, ComparisonKind::LastCommit).await;
    assert_eq!(problem(&changes), ComparisonProblem::Unspecified);
    assert_eq!(paths(&changes), ["sub"]);
    assert_eq!(
        file(&changes, "sub").working_kind(),
        GitEntryKind::Submodule
    );

    // An untracked clone inside the repository.
    let nested = root.join("vendor/nested");
    fs::create_dir_all(&nested).unwrap();
    git(&nested, &["init", "-q", "-b", "main"]);
    write(&nested, "file.txt", "nested\n");
    let changes = list(&mut helper, &root, ComparisonKind::LastCommit).await;
    assert_eq!(problem(&changes), ComparisonProblem::MultipleRepositories);
    fs::remove_dir_all(root.join("vendor")).unwrap();

    // A linked worktree's `.git` is a file; it is still another repository.
    git(&root, &["worktree", "add", "-q", "linked"]);
    let changes = list(&mut helper, &root, ComparisonKind::LastCommit).await;
    assert_eq!(problem(&changes), ComparisonProblem::MultipleRepositories);
}

#[tokio::test(flavor = "multi_thread")]
async fn branch_point_uses_origins_default_when_tracking_another_origin_branch() {
    let (_directory, root) = repository();
    write(&root, "a.txt", "a\n");
    commit(&root, "c1");
    let main = head(&root);
    write(&root, "b.txt", "b\n");
    commit(&root, "c2");
    let release = head(&root);
    add_remote(&root, "origin");
    git(&root, &["update-ref", "refs/remotes/origin/main", &main]);
    git(
        &root,
        &["update-ref", "refs/remotes/origin/release", &release],
    );
    git(
        &root,
        &[
            "symbolic-ref",
            "refs/remotes/origin/HEAD",
            "refs/remotes/origin/main",
        ],
    );
    git(&root, &["checkout", "-q", "-b", "feature"]);
    git(&root, &["config", "branch.feature.remote", "origin"]);
    git(
        &root,
        &["config", "branch.feature.merge", "refs/heads/release"],
    );
    write(&root, "c.txt", "c\n");
    commit(&root, "c3");

    let mut helper = start().await;
    let changes = list(&mut helper, &root, ComparisonKind::BranchPoint).await;
    let base = changes.base.as_ref().unwrap();
    assert_eq!(problem(&changes), ComparisonProblem::Unspecified);
    assert_eq!(base.reference, "origin/main");
    assert_eq!(base.base, main);
}

#[tokio::test(flavor = "multi_thread")]
async fn branch_point_prefers_the_upstream_remote_over_a_push_fork() {
    let (_directory, root, branch_point) = branch_fixture();
    // The branch pushes to a fork whose default branch is something else.
    add_remote(&root, "fork");
    let fork_main = git(&root, &["rev-parse", "feature"]).trim().to_string();
    git(&root, &["update-ref", "refs/remotes/fork/main", &fork_main]);
    git(
        &root,
        &[
            "symbolic-ref",
            "refs/remotes/fork/HEAD",
            "refs/remotes/fork/main",
        ],
    );
    git(&root, &["config", "branch.feature.pushRemote", "fork"]);

    let mut helper = start().await;
    let changes = list(&mut helper, &root, ComparisonKind::BranchPoint).await;
    let base = changes.base.as_ref().unwrap();
    // upstream has no HEAD: its upstream branch wins over the fork's default.
    assert_eq!(base.reference, "upstream/master");
    assert_eq!(base.base, branch_point);
}

#[tokio::test(flavor = "multi_thread")]
async fn listings_carry_working_metadata_that_changes_with_each_edit() {
    let (_directory, root) = repository();
    write(&root, "tracked.txt", "one\n");
    write(&root, "gone.txt", "bye\n");
    commit(&root, "c1");
    write(&root, "tracked.txt", "two\n");
    write(&root, "untracked.txt", "aaaa\n");
    fs::remove_file(root.join("gone.txt")).unwrap();
    symlink("a-long-target-name", root.join("link")).unwrap();

    let mut helper = start().await;
    let before = list(&mut helper, &root, ComparisonKind::LastCommit).await;
    let working =
        |changes: &ListWorkingCopyChangesResponse, path: &str| file(changes, path).working;
    assert_eq!(working(&before, "gone.txt"), None);
    assert_eq!(working(&before, "untracked.txt").unwrap().size, 5);
    // A link's own size, the length of its text.
    assert_eq!(
        working(&before, "link").unwrap().size,
        "a-long-target-name".len() as u64
    );

    // Same sizes and line counts, different content.
    std::thread::sleep(std::time::Duration::from_millis(20));
    write(&root, "tracked.txt", "six\n");
    write(&root, "untracked.txt", "bbbb\n");
    let after = list(&mut helper, &root, ComparisonKind::LastCommit).await;
    for path in ["tracked.txt", "untracked.txt"] {
        let (old, new) = (file(&before, path), file(&after, path));
        assert_eq!(
            (old.status, old.lines_added, old.lines_removed),
            (new.status, new.lines_added, new.lines_removed),
            "{path}"
        );
        assert_ne!(old.working, new.working, "{path}");
    }
    assert_eq!(working(&before, "link"), working(&after, "link"));
}
