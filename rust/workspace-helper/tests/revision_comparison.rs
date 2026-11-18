//! Revision comparisons over the real transport, on temporary repositories:
//! the merge base of a typed base and head, with the working copy or a commit
//! as the head, and the problems a person can cause by typing.

mod common;

use std::{fs, path::Path, path::PathBuf};

use common::{Helper, collect, git, start, write};
use tonic::Code;
use workspace_helper::proto::{
    ChangeStatus, ChangedFile, Comparison, ComparisonField, ComparisonKind, ComparisonProblem,
    GetInfoRequest, ListBranchCommitsRequest, ListBranchCommitsResponse, ListRepositoryRefsRequest,
    ListWorkingCopyChangesRequest, ListWorkingCopyChangesResponse, ReadBaseFileRequest,
};

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

fn rev(root: &Path, revision: &str) -> String {
    git(root, &["rev-parse", revision]).trim().to_string()
}

/// `main` with a.txt, then `feature` adding b.txt and changing a.txt, then
/// `main` moving on with c.txt. Checked out on `feature`.
fn branches() -> (tempfile::TempDir, PathBuf) {
    let directory = tempfile::tempdir().unwrap();
    let root = fs::canonicalize(directory.path()).unwrap();
    git(&root, &["init", "-q", "-b", "main"]);
    write(&root, "a.txt", "one\n");
    commit(&root, "base");
    git(&root, &["tag", "v1"]);
    git(&root, &["checkout", "-q", "-b", "feature"]);
    write(&root, "a.txt", "one\ntwo\n");
    write(&root, "b.txt", "bee\n");
    commit(&root, "feature work");
    git(&root, &["checkout", "-q", "main"]);
    write(&root, "c.txt", "sea\n");
    commit(&root, "main moves on");
    git(&root, &["checkout", "-q", "feature"]);
    (directory, root)
}

async fn compare(
    helper: &mut Helper,
    root: &Path,
    base: &str,
    head: &str,
) -> ListWorkingCopyChangesResponse {
    helper.open(root).await;
    helper
        .client
        .list_working_copy_changes(ListWorkingCopyChangesRequest {
            comparison: Some(Comparison {
                kind: ComparisonKind::Revisions as i32,
                base_revision: base.to_string(),
                head_revision: head.to_string(),
            }),
        })
        .await
        .unwrap()
        .into_inner()
}

fn paths(changes: &ListWorkingCopyChangesResponse) -> Vec<&str> {
    changes
        .files
        .iter()
        .map(|file| file.path.as_str())
        .collect()
}

fn file<'a>(changes: &'a ListWorkingCopyChangesResponse, path: &str) -> &'a ChangedFile {
    changes
        .files
        .iter()
        .find(|file| file.path == path)
        .unwrap_or_else(|| panic!("{path} not listed in {:?}", paths(changes)))
}

/// The problem a comparison reported, and which field it is about.
fn problem(changes: &ListWorkingCopyChangesResponse) -> (ComparisonProblem, ComparisonField) {
    let unavailable = changes
        .base
        .as_ref()
        .unwrap()
        .unavailable
        .clone()
        .unwrap_or_default();
    (unavailable.problem(), unavailable.field())
}

async fn read(helper: &mut Helper, root: &Path, commit: &str, path: &str, object: &str) -> Vec<u8> {
    helper.open(root).await;
    let chunks = collect(
        helper
            .client
            .read_base_file(ReadBaseFileRequest {
                commit: commit.to_string(),
                path: path.to_string(),
                object: object.to_string(),
                maximum_bytes: 0,
            })
            .await
            .unwrap()
            .into_inner(),
    )
    .await
    .unwrap();
    chunks.iter().flat_map(|chunk| chunk.data.clone()).collect()
}

#[tokio::test(flavor = "multi_thread")]
async fn the_helper_says_it_compares_revisions() {
    let mut helper = start().await;
    let info = helper
        .client
        .get_info(GetInfoRequest {})
        .await
        .unwrap()
        .into_inner();
    assert!(
        info.capabilities
            .iter()
            .any(|name| name == "revision_comparison")
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn a_base_and_the_working_copy_compare_from_their_merge_base() {
    let (_directory, root) = branches();
    write(&root, "untracked.txt", "new\n");
    write(&root, "b.txt", "bee\nedited\n");
    let mut helper = start().await;
    let changes = compare(&mut helper, &root, "main", "").await;
    assert_eq!(problem(&changes).0, ComparisonProblem::Unspecified);
    let base = changes.base.as_ref().unwrap();
    // The merge base is where feature left main, not main's newer tip.
    assert_eq!(base.base, rev(&root, "v1"));
    assert_eq!(base.merge_base, rev(&root, "v1"));
    assert_eq!(base.head, rev(&root, "HEAD"));
    assert_eq!(base.base_display, "main");
    assert_eq!(base.head_display, "");
    assert!(base.head_is_working_copy);
    // Committed, edited and untracked files; main's own c.txt isn't a change here.
    assert_eq!(paths(&changes), ["a.txt", "b.txt", "untracked.txt"]);
    assert!(file(&changes, "b.txt").committed.is_none());
}

#[tokio::test(flavor = "multi_thread")]
async fn a_committed_head_lists_only_what_its_commits_changed_and_reads_both_blobs() {
    let (_directory, root) = branches();
    write(&root, "untracked.txt", "new\n");
    write(&root, "a.txt", "dirty working copy\n");
    let mut helper = start().await;
    let changes = compare(&mut helper, &root, "main", "feature").await;
    assert_eq!(problem(&changes).0, ComparisonProblem::Unspecified);
    let base = changes.base.as_ref().unwrap();
    assert_eq!(base.merge_base, rev(&root, "v1"));
    assert_eq!(base.head, rev(&root, "feature"));
    assert_eq!(base.head_display, "feature");
    assert!(!base.head_is_working_copy);
    // Neither the untracked file nor the working copy's edit belongs to the commits.
    assert_eq!(paths(&changes), ["a.txt", "b.txt"]);
    let added = file(&changes, "b.txt");
    assert_eq!(added.status(), ChangeStatus::Added);
    assert!(added.working.is_none());
    let modified = file(&changes, "a.txt");
    assert_eq!(
        (modified.lines_added, modified.lines_removed),
        (Some(1), Some(0))
    );
    let head_blob = modified.committed.clone().unwrap();
    assert_eq!(head_blob.size, Some(8));
    let before = read(
        &mut helper,
        &root,
        &base.merge_base,
        "a.txt",
        &modified.base_object,
    )
    .await;
    let after = read(&mut helper, &root, &base.head, "a.txt", &head_blob.object).await;
    assert_eq!(before, b"one\n");
    assert_eq!(
        after, b"one\ntwo\n",
        "the head's blob, not the dirty working file"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn an_empty_base_means_the_default_branch_else_head() {
    let (_directory, root) = branches();
    let mut helper = start().await;
    // No remote, so no default branch: the base is HEAD itself.
    let changes = compare(&mut helper, &root, "", "").await;
    let base = changes.base.as_ref().unwrap();
    assert_eq!(base.base_display, "HEAD");
    assert_eq!(base.merge_base, rev(&root, "HEAD"));
    assert!(changes.files.is_empty());
}

/// `origin` configured as a clone does, its default branch at the fork point.
fn with_origin_default_branch(root: &Path) {
    git(
        root,
        &[
            "config",
            "remote.origin.url",
            "https://example.invalid/origin.git",
        ],
    );
    git(
        root,
        &[
            "config",
            "remote.origin.fetch",
            "+refs/heads/*:refs/remotes/origin/*",
        ],
    );
    let fork_point = rev(root, "v1");
    git(
        root,
        &["update-ref", "refs/remotes/origin/main", &fork_point],
    );
    git(
        root,
        &[
            "symbolic-ref",
            "refs/remotes/origin/HEAD",
            "refs/remotes/origin/main",
        ],
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn an_empty_base_is_the_detected_default_branch_even_when_a_tag_shares_its_name() {
    let (_directory, root) = branches();
    with_origin_default_branch(&root);
    // A tag named like the default branch's short name, at feature's tip.
    git(&root, &["tag", "origin/main", "feature"]);
    let mut helper = start().await;
    let changes = compare(&mut helper, &root, "", "").await;
    assert_eq!(problem(&changes).0, ComparisonProblem::Unspecified);
    let base = changes.base.as_ref().unwrap();
    assert_eq!(base.base_display, "origin/main");
    assert_eq!(
        base.reference_commit,
        rev(&root, "refs/remotes/origin/main")
    );
    assert_eq!(base.merge_base, rev(&root, "v1"));
    assert_eq!(paths(&changes), ["a.txt", "b.txt"]);
}

#[tokio::test(flavor = "multi_thread")]
async fn refs_leave_out_only_remote_heads_and_name_the_default_branch() {
    let (_directory, root) = branches();
    with_origin_default_branch(&root);
    git(&root, &["branch", "topic/HEAD", "main"]);
    git(&root, &["tag", "release/HEAD", "main"]);
    let mut helper = start().await;
    helper.open(&root).await;
    let refs = helper
        .client
        .list_repository_refs(ListRepositoryRefsRequest {})
        .await
        .unwrap()
        .into_inner();
    assert_eq!(
        refs.refs,
        [
            "feature",
            "main",
            "topic/HEAD",
            "origin/main",
            "release/HEAD",
            "v1"
        ]
    );
    assert!(!refs.truncated);
    assert_eq!(refs.default_branch_ref, "origin/main");
}

#[tokio::test(flavor = "multi_thread")]
async fn revisions_that_could_be_options_never_reach_git() {
    let (_directory, root) = branches();
    let mut helper = start().await;
    let output = root.join("leaked.txt");
    let changes = compare(
        &mut helper,
        &root,
        &format!("--output={}", output.display()),
        "",
    )
    .await;
    assert_eq!(
        problem(&changes),
        (ComparisonProblem::InvalidRevision, ComparisonField::Base)
    );
    assert!(!output.exists(), "git never ran with the option");
    let changes = compare(&mut helper, &root, "main", "-p").await;
    assert_eq!(
        problem(&changes),
        (ComparisonProblem::InvalidRevision, ComparisonField::Head)
    );
    let changes = compare(&mut helper, &root, "main\nHEAD", "").await;
    assert_eq!(problem(&changes).0, ComparisonProblem::InvalidRevision);
}

#[tokio::test(flavor = "multi_thread")]
async fn unknown_revisions_name_the_field_they_are_in() {
    let (_directory, root) = branches();
    let mut helper = start().await;
    assert_eq!(
        problem(&compare(&mut helper, &root, "nope", "").await),
        (ComparisonProblem::BaseNotFound, ComparisonField::Base)
    );
    assert_eq!(
        problem(&compare(&mut helper, &root, "main", "nope").await),
        (ComparisonProblem::HeadNotFound, ComparisonField::Head)
    );
    assert_eq!(
        problem(&compare(&mut helper, &root, "nope", "nada").await),
        (ComparisonProblem::BaseNotFound, ComparisonField::Both)
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn revisions_with_no_shared_commit_say_so_for_both() {
    let (_directory, root) = branches();
    git(&root, &["checkout", "-q", "--orphan", "unrelated"]);
    git(&root, &["rm", "-rq", "--cached", "."]);
    write(&root, "z.txt", "zed\n");
    commit(&root, "unrelated root");
    let mut helper = start().await;
    assert_eq!(
        problem(&compare(&mut helper, &root, "main", "unrelated").await),
        (ComparisonProblem::NoCommonHistory, ComparisonField::Both)
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn refs_list_branches_and_tags() {
    let (_directory, root) = branches();
    let mut helper = start().await;
    helper.open(&root).await;
    let refs = helper
        .client
        .list_repository_refs(ListRepositoryRefsRequest {})
        .await
        .unwrap()
        .into_inner();
    assert_eq!(refs.refs, ["feature", "main", "v1"]);
    assert!(!refs.truncated);
    assert_eq!(refs.default_branch_ref, "");
}

#[tokio::test(flavor = "multi_thread")]
async fn refs_outside_a_repository_are_a_precondition_failure() {
    let directory = tempfile::tempdir().unwrap();
    let root = fs::canonicalize(directory.path()).unwrap();
    let mut helper = start().await;
    helper.open(&root).await;
    let error = helper
        .client
        .list_repository_refs(ListRepositoryRefsRequest {})
        .await
        .unwrap_err();
    assert_eq!(error.code(), Code::FailedPrecondition);
}

/// Every file under `directory`, with its bytes and modification time.
fn snapshot(directory: &Path) -> Vec<(PathBuf, Vec<u8>, std::time::SystemTime)> {
    let mut found = Vec::new();
    let mut pending = vec![directory.to_path_buf()];
    while let Some(next) = pending.pop() {
        for entry in fs::read_dir(&next).unwrap().flatten() {
            let path = entry.path();
            let metadata = fs::symlink_metadata(&path).unwrap();
            if metadata.is_dir() {
                pending.push(path);
            } else {
                let bytes = fs::read(&path).unwrap_or_default();
                found.push((path, bytes, metadata.modified().unwrap()));
            }
        }
    }
    found.sort_by(|left, right| left.0.cmp(&right.0));
    found
}

#[tokio::test(flavor = "multi_thread")]
async fn revision_comparisons_only_read_the_repository() {
    let (_directory, root) = branches();
    write(&root, "untracked.txt", "new\n");
    let before = snapshot(&root.join(".git"));
    let mut helper = start().await;
    let committed = compare(&mut helper, &root, "main", "feature").await;
    compare(&mut helper, &root, "v1", "").await;
    helper.open(&root).await;
    helper
        .client
        .list_repository_refs(ListRepositoryRefsRequest {})
        .await
        .unwrap();
    let a = file(&committed, "a.txt");
    let head = committed.base.as_ref().unwrap().head.clone();
    read(
        &mut helper,
        &root,
        &head,
        "a.txt",
        &a.committed.clone().unwrap().object,
    )
    .await;
    assert_eq!(
        snapshot(&root.join(".git")),
        before,
        "nothing under .git changed"
    );
}

async fn branch_commits(helper: &mut Helper, root: &Path, base: &str) -> ListBranchCommitsResponse {
    helper.open(root).await;
    helper
        .client
        .list_branch_commits(ListBranchCommitsRequest {
            base_revision: base.to_string(),
        })
        .await
        .unwrap()
        .into_inner()
}

fn subjects(listed: &ListBranchCommitsResponse) -> Vec<&str> {
    listed
        .commits
        .iter()
        .map(|commit| commit.subject.as_str())
        .collect()
}

#[tokio::test(flavor = "multi_thread")]
async fn branch_commits_run_from_the_default_branchs_merge_base_newest_first() {
    let (_directory, root) = branches();
    with_origin_default_branch(&root);
    commit(&root, "more feature work");
    let mut helper = start().await;
    let info = helper
        .client
        .get_info(GetInfoRequest {})
        .await
        .unwrap()
        .into_inner();
    assert!(
        info.capabilities
            .iter()
            .any(|name| name == "branch_commits")
    );
    let listed = branch_commits(&mut helper, &root, "").await;
    assert_eq!(subjects(&listed), ["more feature work", "feature work"]);
    assert!(!listed.truncated);
    let newest = &listed.commits[0];
    assert_eq!(newest.object, rev(&root, "HEAD"));
    assert_eq!(newest.author_name, "Test");
    assert!(newest.author_time_unix > 0);
    let base = listed.base.as_ref().unwrap();
    assert_eq!(base.base_display, "origin/main");
    assert_eq!(base.merge_base, rev(&root, "v1"));
    // A named base counts from its own merge base.
    let since_feature = branch_commits(&mut helper, &root, "feature~1").await;
    assert_eq!(subjects(&since_feature), ["more feature work"]);
}

#[tokio::test(flavor = "multi_thread")]
async fn branch_commits_follow_a_detached_head_and_are_empty_on_the_base_itself() {
    let (_directory, root) = branches();
    let mut helper = start().await;
    git(&root, &["checkout", "-q", "--detach", "feature"]);
    assert_eq!(
        subjects(&branch_commits(&mut helper, &root, "main").await),
        ["feature work"]
    );
    git(&root, &["checkout", "-q", "main"]);
    let on_main = branch_commits(&mut helper, &root, "main").await;
    assert!(on_main.commits.is_empty());
    assert!(on_main.base.unwrap().unavailable.is_none());
}

#[tokio::test(flavor = "multi_thread")]
async fn branch_commits_stop_at_the_limit_and_say_so() {
    let (_directory, root) = branches();
    for index in 0..200 {
        commit(&root, &format!("step {index}"));
    }
    let mut helper = start().await;
    let listed = branch_commits(&mut helper, &root, "main").await;
    assert_eq!(listed.commits.len(), 200);
    assert!(listed.truncated);
    assert_eq!(listed.commits[0].subject, "step 199");
}

#[tokio::test(flavor = "multi_thread")]
async fn branch_commits_report_a_base_that_isnt_there_or_could_be_an_option() {
    let (_directory, root) = branches();
    let mut helper = start().await;
    for (base, expected) in [
        ("no-such-branch", ComparisonProblem::BaseNotFound),
        ("--output=leaked.txt", ComparisonProblem::InvalidRevision),
    ] {
        let listed = branch_commits(&mut helper, &root, base).await;
        assert!(listed.commits.is_empty(), "{base}");
        let unavailable = listed.base.unwrap().unavailable.unwrap();
        assert_eq!(unavailable.problem(), expected, "{base}");
        assert_eq!(unavailable.field(), ComparisonField::Base, "{base}");
    }
    assert!(!root.join("leaked.txt").exists());
}

#[tokio::test(flavor = "multi_thread")]
async fn a_root_commit_merged_from_unrelated_history_is_listed_and_compares_with_the_empty_tree() {
    let (_directory, root) = branches();
    git(&root, &["checkout", "-q", "--orphan", "imported"]);
    git(&root, &["rm", "-q", "-rf", "."]);
    write(&root, "imported.txt", "from elsewhere\n");
    commit(&root, "imported root");
    let imported_root = rev(&root, "HEAD");
    git(&root, &["checkout", "-q", "feature"]);
    git(
        &root,
        &[
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "merge",
            "-q",
            "--allow-unrelated-histories",
            "-m",
            "merge imported",
            "imported",
        ],
    );
    let mut helper = start().await;
    let listed = branch_commits(&mut helper, &root, "main").await;
    // Commits made within one second have no defined order between them.
    let mut roots: Vec<(&str, bool)> = listed
        .commits
        .iter()
        .map(|commit| (commit.subject.as_str(), commit.root))
        .collect();
    roots.sort_unstable();
    assert_eq!(
        roots,
        [
            ("feature work", false),
            ("imported root", true),
            ("merge imported", false)
        ]
    );
    // Its parent doesn't exist, so the empty tree stands in for it.
    let parent = compare(
        &mut helper,
        &root,
        &format!("{imported_root}~1"),
        &imported_root,
    )
    .await;
    assert_eq!(
        problem(&parent),
        (ComparisonProblem::BaseNotFound, ComparisonField::Base)
    );
    let empty_tree = "4b825dc642cb6eb9a060e54bf8d69288fbee4904";
    // A head with parents can't be shown against the empty tree, as if all of it were added.
    // HEAD is the merge; its first parent an ordinary commit.
    for head in [rev(&root, "HEAD"), rev(&root, "HEAD^1")] {
        let head = head.as_str();
        let refused = compare(&mut helper, &root, empty_tree, head).await;
        assert!(refused.files.is_empty(), "{head}");
        assert_eq!(
            problem(&refused),
            (ComparisonProblem::BaseNotFound, ComparisonField::Base),
            "{head}"
        );
    }
    let changes = compare(&mut helper, &root, empty_tree, &imported_root).await;
    assert_eq!(paths(&changes), ["imported.txt"]);
    let added = file(&changes, "imported.txt");
    assert_eq!(added.status(), ChangeStatus::Added);
    let base = changes.base.as_ref().unwrap();
    assert_eq!(base.head, imported_root);
    assert_eq!(base.merge_base, empty_tree);
    let committed = added.committed.as_ref().unwrap();
    assert_eq!(
        read(
            &mut helper,
            &root,
            &imported_root,
            "imported.txt",
            &committed.object
        )
        .await,
        b"from elsewhere\n"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn a_shallow_clones_boundary_commit_is_not_shown_against_the_empty_tree() {
    let (_source, source) = branches();
    let directory = tempfile::tempdir().unwrap();
    let root = fs::canonicalize(directory.path()).unwrap().join("shallow");
    git(
        directory.path(),
        &[
            "clone",
            "-q",
            "--depth",
            "1",
            "--branch",
            "feature",
            &format!("file://{}", source.display()),
            root.to_str().unwrap(),
        ],
    );
    // The clone hides the tip's parent, though the commit still names it.
    assert!(git(&root, &["log", "-1", "--format=%P"]).trim().is_empty());
    let mut helper = start().await;
    let refused = compare(
        &mut helper,
        &root,
        "4b825dc642cb6eb9a060e54bf8d69288fbee4904",
        &rev(&root, "HEAD"),
    )
    .await;
    assert!(refused.files.is_empty());
    assert_eq!(
        problem(&refused),
        (ComparisonProblem::BaseNotFound, ComparisonField::Base)
    );
}
