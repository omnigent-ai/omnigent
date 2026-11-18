use super::*;

use crate::git_runner::{fake_discovery, fake_git};

/// A repository whose Git answers discovery, then runs `rest` for every
/// other command.
async fn fake_repository(rest: &str) -> (tempfile::TempDir, GitRepository) {
    let directory = tempfile::tempdir().unwrap();
    let root = std::fs::canonicalize(directory.path()).unwrap();
    fake_git(&format!("{}{rest}", fake_discovery(&root)));
    let repository = open_repository(&root, Instant::now() + Duration::from_secs(20))
        .await
        .unwrap();
    (directory, repository)
}

fn deadline() -> Instant {
    Instant::now() + Duration::from_secs(20)
}

/// The base lookup failed because Git was signalled, naming the command and its runtime.
fn assert_signalled<T: std::fmt::Debug>(result: Result<T, Unavailable>, prefix: &str) {
    match result {
        Err(Unavailable::GitFailed(message)) => assert!(
            message.starts_with(prefix) && message.ends_with(" ms"),
            "{message}"
        ),
        other => panic!("expected a signalled Git failure, got {other:?}"),
    }
}

const HEAD: &str = "1111111111111111111111111111111111111111";
const MAIN: &str = "2222222222222222222222222222222222222222";

#[tokio::test(flavor = "current_thread")]
async fn a_signalled_head_lookup_is_an_error_not_an_empty_repository() {
    let (_directory, repository) = fake_repository(r#"kill -KILL $$"#).await;
    assert_signalled(
        resolve_base(&repository, ComparisonKind::LastCommit, deadline()).await,
        "git rev-parse was stopped by signal 9 after ",
    );
}

#[tokio::test(flavor = "current_thread")]
async fn a_missing_head_is_an_empty_repository() {
    let (_directory, repository) = fake_repository("exit 1").await;
    assert_eq!(
        resolve_base(&repository, ComparisonKind::LastCommit, deadline()).await,
        Err(Unavailable::NoCommits)
    );
}

#[tokio::test(flavor = "current_thread")]
async fn a_signalled_branch_lookup_is_an_error_not_a_detached_head() {
    let (_directory, repository) = fake_repository(&format!(
        r#"case "$*" in
*rev-parse\ --verify*) echo {HEAD}; exit 0;;
*symbolic-ref\ -q\ HEAD*) kill -KILL $$;;
esac
exit 1"#
    ))
    .await;
    assert_signalled(
        resolve_base(&repository, ComparisonKind::BranchPoint, deadline()).await,
        "git symbolic-ref was stopped by signal 9 after ",
    );
}

#[tokio::test(flavor = "current_thread")]
async fn merge_base_failures_keep_their_kind() {
    let script = |merge_base: &str| {
        format!(
            r#"case "$*" in
*rev-parse\ --verify*HEAD*) echo {HEAD}; exit 0;;
*rev-parse\ --verify*) echo {MAIN}; exit 0;;
*symbolic-ref\ -q\ HEAD*) echo refs/heads/feature; exit 0;;
*for-each-ref*) printf 'refs/heads/feature\0\0\0\0\0\n'; exit 0;;
*symbolic-ref\ -q\ refs/remotes/origin/HEAD*) echo refs/remotes/origin/main; exit 0;;
*merge-base*) {merge_base};;
*is-shallow*) echo false; exit 0;;
esac
exit 1"#
        )
    };
    let (_directory, repository) = fake_repository(&script("kill -KILL $$")).await;
    assert_signalled(
        resolve_base(&repository, ComparisonKind::BranchPoint, deadline()).await,
        "git merge-base was stopped by signal 9 after ",
    );
    let (_directory, repository) =
        fake_repository(&script("echo 'fatal: missing blob object' >&2; exit 128")).await;
    assert_eq!(
        resolve_base(&repository, ComparisonKind::BranchPoint, deadline()).await,
        Err(Unavailable::MissingObject)
    );
    // Exit 1 with nothing on stderr: the histories don't meet.
    let (_directory, repository) = fake_repository(&script("exit 1")).await;
    assert_eq!(
        resolve_base(&repository, ComparisonKind::BranchPoint, deadline()).await,
        Err(Unavailable::NoBranchPoint {
            reference: "origin/main".into(),
            shallow: false
        })
    );
}

#[tokio::test(flavor = "current_thread")]
async fn a_base_read_shares_the_deadline_discovery_started() {
    let (_directory, repository) =
        fake_repository(&format!("echo '100644 blob {MAIN}\tf'; exit 0")).await;
    // Discovery used the whole budget: the read must not start a fresh one.
    let spent = Instant::now();
    assert_eq!(
        read_base_blob(&repository, HEAD, "f", MAIN, 1024, spent).await,
        Err(ReadError::Git(Unavailable::Timeout))
    );
}

#[test]
fn parses_raw_records_with_modes_and_full_objects() {
    let old = "a".repeat(40);
    let zero = "0".repeat(40);
    let raw = format!(
        ":100644 100644 {old} {zero} M\0src/a.rs\0:100644 000000 {old} {zero} D\0gone.txt\0:100644 120000 {old} {zero} T\0link\0:160000 160000 {old} {zero} M\0vendor/lib\0"
    );
    let mut unrepresentable = 0;
    let entries = parse_raw_diff(raw.as_bytes(), &mut unrepresentable, false).unwrap();
    assert_eq!(entries.len(), 4);
    assert_eq!(entries[0].status, ChangeStatus::Modified);
    assert_eq!(entries[0].base_object.as_deref(), Some(old.as_str()));
    assert_eq!(entries[1].status, ChangeStatus::Deleted);
    assert_eq!(entries[1].working_kind, None);
    assert_eq!(entries[2].status, ChangeStatus::TypeChanged);
    assert_eq!(entries[2].working_kind, Some(EntryKind::Symlink));
    assert_eq!(entries[3].base_kind, Some(EntryKind::Submodule));
}

#[test]
fn splits_long_path_lists_into_bounded_batches() {
    let long = "p".repeat(40_000);
    let paths = [long.as_str(), long.as_str(), "short"];
    let batches = argument_batches(&paths);
    assert_eq!(batches.len(), 2);
    assert_eq!(batches[1], [long.clone(), "short".to_string()]);
    assert!(argument_batches(&[]).is_empty());
}

#[test]
fn counts_names_that_are_not_utf8_instead_of_listing_them() {
    let old = "b".repeat(40);
    let zero = "0".repeat(40);
    let mut raw = format!(":100644 100644 {old} {zero} M\0").into_bytes();
    raw.extend_from_slice(b"bad\xff\0");
    let mut unrepresentable = 0;
    assert!(
        parse_raw_diff(&raw, &mut unrepresentable, false)
            .unwrap()
            .is_empty()
    );
    assert_eq!(unrepresentable, 1);
}
