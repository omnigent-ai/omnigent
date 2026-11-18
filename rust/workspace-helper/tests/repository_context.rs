//! Repository context over the real transport, on temporary repositories with
//! real Git: where the checked-out branch pushes, and the remotes, as
//! host/owner/repo. Nothing here reaches a network: remote URLs are never
//! contacted, and tracking refs are written directly.

mod common;

use std::{
    fs,
    path::{Path, PathBuf},
    sync::OnceLock,
};

use common::{git, start, write};
use workspace_helper::proto::{
    PushUnresolved, RepositoryContextProblem, ResolveRepositoryContextRequest,
    ResolveRepositoryContextResponse,
};

/// One home for the whole binary, so tests control the global configuration
/// the helper's Git reads; tests that write it hold the lock.
fn home() -> &'static Path {
    static HOME: OnceLock<PathBuf> = OnceLock::new();
    HOME.get_or_init(|| {
        let directory = tempfile::tempdir().unwrap().keep();
        let home = fs::canonicalize(directory).unwrap();
        // SAFETY: set once, before any helper or Git starts in this binary.
        unsafe {
            std::env::set_var("HOME", &home);
            std::env::set_var("XDG_CONFIG_HOME", home.join(".config"));
            std::env::remove_var("GIT_CONFIG_GLOBAL");
            std::env::set_var("GIT_CONFIG_NOSYSTEM", "1");
        }
        home
    })
}

async fn serialized() -> tokio::sync::MutexGuard<'static, ()> {
    static LOCK: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());
    let guard = LOCK.lock().await;
    let _ = fs::remove_file(home().join(".gitconfig"));
    guard
}

fn global_config(contents: &str) {
    fs::write(home().join(".gitconfig"), contents).unwrap();
}

/// A repository on branch `feature` with one commit; returns its directory and root.
fn repository() -> (tempfile::TempDir, PathBuf) {
    let directory = tempfile::tempdir().unwrap();
    let root = fs::canonicalize(directory.path()).unwrap();
    git(&root, &["init", "-q", "-b", "main"]);
    write(&root, "README.md", "hello\n");
    git(&root, &["add", "README.md"]);
    commit(&root, "first");
    git(&root, &["checkout", "-q", "-b", "feature"]);
    (directory, root)
}

fn commit(root: &Path, message: &str) {
    git(
        root,
        &[
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            message,
        ],
    );
}

fn config(root: &Path, pairs: &[(&str, &str)]) {
    for (key, value) in pairs {
        git(root, &["config", "--add", key, value]);
    }
}

fn add_remote(root: &Path, name: &str, url: &str) {
    git(root, &["remote", "add", name, url]);
}

fn head(root: &Path) -> String {
    git(root, &["rev-parse", "HEAD"]).trim().to_string()
}

/// Writes a tracking ref, as a fetch would have, without any network.
fn track(root: &Path, reference: &str) {
    git(root, &["update-ref", reference, "HEAD"]);
}

async fn context_of(root: &Path) -> ResolveRepositoryContextResponse {
    home();
    let mut helper = start().await;
    helper.open(root).await;
    helper
        .client
        .resolve_repository_context(ResolveRepositoryContextRequest {})
        .await
        .unwrap()
        .into_inner()
}

fn problem(context: &ResolveRepositoryContextResponse) -> RepositoryContextProblem {
    RepositoryContextProblem::try_from(context.problem).unwrap()
}

fn unresolved(context: &ResolveRepositoryContextResponse) -> PushUnresolved {
    PushUnresolved::try_from(context.push_unresolved).unwrap()
}

fn push_summary(
    context: &ResolveRepositoryContextResponse,
) -> Option<(String, String, String, String)> {
    context.push.as_ref().map(|push| {
        (
            push.remote_name.clone(),
            push.branch.clone(),
            format!("{}/{}/{}", push.host, push.owner, push.repo),
            push.tracking_sha.clone(),
        )
    })
}

#[tokio::test]
async fn reports_a_branch_pushing_to_its_own_upstream() {
    let _guard = serialized().await;
    let (_directory, root) = repository();
    add_remote(&root, "origin", "https://github.com/acme/app.git");
    config(
        &root,
        &[
            ("branch.feature.remote", "origin"),
            ("branch.feature.merge", "refs/heads/feature"),
        ],
    );
    track(&root, "refs/remotes/origin/feature");
    let context = context_of(&root).await;
    assert_eq!(problem(&context), RepositoryContextProblem::Unspecified);
    assert_eq!(context.branch, "feature");
    assert_eq!(context.head_sha, head(&root));
    assert_eq!(context.upstream_ref, "refs/remotes/origin/feature");
    assert_eq!(
        push_summary(&context),
        Some((
            "origin".into(),
            "feature".into(),
            "github.com/acme/app".into(),
            head(&root)
        ))
    );
    let remotes: Vec<_> = context
        .remotes
        .iter()
        .map(|remote| {
            format!(
                "{}={}/{}/{}",
                remote.name, remote.host, remote.owner, remote.repo
            )
        })
        .collect();
    assert_eq!(remotes, vec!["origin=github.com/acme/app"]);
}

#[tokio::test]
async fn follows_a_triangular_workflow_to_the_fork() {
    let _guard = serialized().await;
    let (_directory, root) = repository();
    add_remote(&root, "upstream", "https://github.com/acme/app.git");
    add_remote(&root, "fork", "git@github.com:me/app.git");
    config(
        &root,
        &[
            ("branch.feature.remote", "upstream"),
            ("branch.feature.merge", "refs/heads/main"),
            ("remote.pushDefault", "fork"),
        ],
    );
    track(&root, "refs/remotes/fork/feature");
    let context = context_of(&root).await;
    // The upstream is main, but the branch pushes to feature on the fork.
    assert_eq!(context.upstream_ref, "refs/remotes/upstream/main");
    assert_eq!(
        push_summary(&context),
        Some((
            "fork".into(),
            "feature".into(),
            "github.com/me/app".into(),
            head(&root)
        ))
    );
    assert_eq!(context.remotes.len(), 2);
}

#[tokio::test]
async fn gives_no_push_evidence_where_git_would_refuse_the_push() {
    let _guard = serialized().await;
    // Central, default simple, tracking another branch.
    let (_tracking, root) = repository();
    add_remote(&root, "origin", "https://github.com/acme/app");
    config(
        &root,
        &[
            ("branch.feature.remote", "origin"),
            ("branch.feature.merge", "refs/heads/main"),
        ],
    );
    let context = context_of(&root).await;
    assert_eq!(context.push, None);
    assert_eq!(unresolved(&context), PushUnresolved::Unspecified);
    // Only origin, no upstream at all: still central, and Git refuses.
    let (_plain, root) = repository();
    add_remote(&root, "origin", "https://github.com/acme/app");
    assert_eq!(context_of(&root).await.push, None);
    // A merge without a remote is no upstream: Git refuses that push too.
    let (_merge_only, root) = repository();
    add_remote(&root, "origin", "https://github.com/acme/app");
    config(&root, &[("branch.feature.merge", "refs/heads/feature")]);
    track(&root, "refs/remotes/origin/feature");
    assert_eq!(context_of(&root).await.push, None);
    // push.default=upstream with two upstream branches: Git refuses, though %(push) names one.
    let (_two, root) = repository();
    add_remote(&root, "origin", "https://github.com/acme/app");
    config(
        &root,
        &[
            ("push.default", "upstream"),
            ("branch.feature.remote", "origin"),
            ("branch.feature.merge", "refs/heads/main"),
            ("branch.feature.merge", "refs/heads/other"),
        ],
    );
    track(&root, "refs/remotes/origin/main");
    assert_eq!(context_of(&root).await.push, None);
    // autoSetupRemote without an upstream isn't modeled.
    let (_auto, root) = repository();
    add_remote(&root, "origin", "https://github.com/acme/app");
    config(
        &root,
        &[
            ("push.default", "current"),
            ("push.autoSetupRemote", "true"),
        ],
    );
    assert_eq!(context_of(&root).await.push, None);
}

#[tokio::test]
async fn maps_renamed_tracking_refs_and_push_refspecs_to_the_remote_branch() {
    let _guard = serialized().await;
    let (_renamed, root) = repository();
    add_remote(&root, "origin", "https://github.com/acme/app");
    git(
        &root,
        &[
            "config",
            "remote.origin.fetch",
            "+refs/heads/*:refs/remotes/origin/cache/*",
        ],
    );
    config(&root, &[("push.default", "current")]);
    track(&root, "refs/remotes/origin/cache/feature");
    let context = context_of(&root).await;
    assert_eq!(
        push_summary(&context),
        Some((
            "origin".into(),
            "feature".into(),
            "github.com/acme/app".into(),
            head(&root)
        ))
    );

    let (_refspec, root) = repository();
    add_remote(&root, "origin", "https://github.com/acme/app");
    config(
        &root,
        &[(
            "remote.origin.push",
            "refs/heads/feature:refs/heads/review/feature",
        )],
    );
    let context = context_of(&root).await;
    let push = context.push.unwrap();
    assert_eq!(push.branch, "review/feature");
    // No local tracking ref exists for it yet.
    assert_eq!(push.tracking_sha, "");

    // A full source beside a short spelling of the same branch pushes to both.
    let (_mixed, root) = repository();
    add_remote(&root, "origin", "https://github.com/acme/app");
    config(
        &root,
        &[
            (
                "remote.origin.push",
                "refs/heads/feature:refs/heads/review-a",
            ),
            ("remote.origin.push", "heads/feature:refs/heads/review-b"),
        ],
    );
    let context = context_of(&root).await;
    assert_eq!(context.push, None);
    assert_eq!(unresolved(&context), PushUnresolved::Refspec);

    // A short refspec pushes too, but Git reports no target for it, so neither do we.
    let (_short, root) = repository();
    add_remote(&root, "origin", "https://github.com/acme/app");
    config(&root, &[("remote.origin.push", "feature:review/feature")]);
    let context = context_of(&root).await;
    assert_eq!(context.push, None);
    assert_eq!(unresolved(&context), PushUnresolved::Refspec);

    let (_two, root) = repository();
    add_remote(&root, "origin", "https://github.com/acme/app");
    config(
        &root,
        &[
            (
                "remote.origin.push",
                "refs/heads/feature:refs/heads/review-a",
            ),
            (
                "remote.origin.push",
                "refs/heads/feature:refs/heads/review-b",
            ),
        ],
    );
    let context = context_of(&root).await;
    assert_eq!(context.push, None);
    assert_eq!(unresolved(&context), PushUnresolved::Refspec);
}

#[tokio::test]
async fn picks_the_push_remote_as_git_does() {
    let _guard = serialized().await;
    // A remote name with a slash, chosen by pushRemote.
    let (_slash, root) = repository();
    add_remote(&root, "origin", "https://github.com/acme/app");
    add_remote(&root, "team/fork", "https://github.com/team/app");
    config(
        &root,
        &[
            ("push.default", "current"),
            ("branch.feature.pushRemote", "team/fork"),
        ],
    );
    track(&root, "refs/remotes/team/fork/feature");
    let push = context_of(&root).await.push.unwrap();
    assert_eq!(
        (push.remote_name.as_str(), push.owner.as_str()),
        ("team/fork", "team")
    );

    // The sole remote stands in for origin.
    let (_sole, root) = repository();
    add_remote(&root, "fork", "https://github.com/me/app");
    config(&root, &[("push.default", "current")]);
    assert_eq!(context_of(&root).await.push.unwrap().remote_name, "fork");

    // A global pushDefault picks the repository's own remote.
    let (_global, root) = repository();
    add_remote(&root, "origin", "https://github.com/acme/app");
    add_remote(&root, "fork", "https://github.com/me/app");
    config(&root, &[("push.default", "current")]);
    global_config("[remote]\n\tpushDefault = fork\n");
    let context = context_of(&root).await;
    assert_eq!(problem(&context), RepositoryContextProblem::Unspecified);
    assert_eq!(context.push.unwrap().remote_name, "fork");
    // Naming a remote the repository doesn't have gives no evidence.
    global_config("[remote]\n\tpushDefault = missing\n");
    assert_eq!(context_of(&root).await.push, None);
}

#[tokio::test]
async fn takes_the_push_repository_from_push_urls() {
    let _guard = serialized().await;
    let (_distinct, root) = repository();
    add_remote(&root, "origin", "https://github.com/acme/app");
    config(
        &root,
        &[
            ("push.default", "current"),
            ("remote.origin.pushurl", "git@github.com:me/app.git"),
        ],
    );
    track(&root, "refs/remotes/origin/feature");
    let push = context_of(&root).await.push.unwrap();
    assert_eq!(format!("{}/{}", push.owner, push.repo), "me/app");
    // The tracking ref describes the fetch side, another repository.
    assert_eq!(push.tracking_sha, "");

    let (_several, root) = repository();
    add_remote(&root, "origin", "https://github.com/acme/app");
    config(
        &root,
        &[
            ("push.default", "current"),
            ("remote.origin.pushurl", "https://github.com/me/app"),
            ("remote.origin.pushurl", "https://github.com/other/app"),
        ],
    );
    let context = context_of(&root).await;
    assert_eq!(context.push, None);
    assert_eq!(unresolved(&context), PushUnresolved::PushUrl);
}

#[tokio::test]
async fn defers_matching_pushes() {
    let _guard = serialized().await;
    let (_directory, root) = repository();
    add_remote(&root, "origin", "https://github.com/acme/app");
    config(&root, &[("push.default", "matching")]);
    let context = context_of(&root).await;
    assert_eq!(context.push, None);
    assert_eq!(unresolved(&context), PushUnresolved::PushDefault);
}

#[tokio::test]
async fn reports_detached_heads_empty_repositories_and_non_repositories() {
    let _guard = serialized().await;
    let (_detached, root) = repository();
    add_remote(&root, "origin", "https://github.com/acme/app");
    let sha = head(&root);
    git(&root, &["checkout", "-q", "--detach"]);
    let context = context_of(&root).await;
    assert_eq!(problem(&context), RepositoryContextProblem::Unspecified);
    assert_eq!(
        (context.branch.as_str(), context.head_sha.as_str()),
        ("", sha.as_str())
    );
    assert_eq!(context.push, None);

    let empty = tempfile::tempdir().unwrap();
    let root = fs::canonicalize(empty.path()).unwrap();
    git(&root, &["init", "-q", "-b", "main"]);
    let context = context_of(&root).await;
    assert_eq!(problem(&context), RepositoryContextProblem::Unspecified);
    assert_eq!(
        (context.branch.as_str(), context.head_sha.as_str()),
        ("main", "")
    );

    let plain = tempfile::tempdir().unwrap();
    let root = fs::canonicalize(plain.path()).unwrap();
    assert_eq!(
        problem(&context_of(&root).await),
        RepositoryContextProblem::NotARepository
    );
}

#[tokio::test]
async fn reads_a_linked_worktree_with_the_shared_remotes() {
    let _guard = serialized().await;
    let (_directory, root) = repository();
    add_remote(&root, "origin", "https://github.com/acme/app");
    let linked = tempfile::tempdir().unwrap();
    let worktree = fs::canonicalize(linked.path()).unwrap().join("wt");
    git(
        &root,
        &[
            "worktree",
            "add",
            "-q",
            "-b",
            "linked",
            worktree.to_str().unwrap(),
        ],
    );
    config(&root, &[("push.default", "current")]);
    track(&root, "refs/remotes/origin/linked");
    let context = context_of(&worktree).await;
    assert_eq!(problem(&context), RepositoryContextProblem::Unspecified);
    assert_eq!(context.branch, "linked");
    assert_eq!(context.remotes.len(), 1);
    assert_eq!(context.push.unwrap().branch, "linked");
}

#[tokio::test]
async fn defers_url_rewrites_and_remotes_from_other_files() {
    let _guard = serialized().await;
    let (_local, root) = repository();
    add_remote(&root, "origin", "gh:acme/app");
    config(&root, &[("url.https://github.com/.insteadOf", "gh:")]);
    assert_eq!(
        problem(&context_of(&root).await),
        RepositoryContextProblem::UnsupportedConfig
    );

    let (_global, root) = repository();
    add_remote(&root, "origin", "https://github.com/acme/app");
    global_config("[url \"git@github.com:\"]\n\tpushInsteadOf = https://github.com/\n");
    assert_eq!(
        problem(&context_of(&root).await),
        RepositoryContextProblem::UnsupportedConfig
    );
    global_config("");

    let (_included, root) = repository();
    let extra = root.join(".git").join("extra-remotes");
    fs::write(
        &extra,
        "[remote \"fork\"]\n\turl = https://github.com/me/app\n",
    )
    .unwrap();
    config(&root, &[("include.path", extra.to_str().unwrap())]);
    assert_eq!(
        problem(&context_of(&root).await),
        RepositoryContextProblem::UnsupportedConfig
    );

    // An unrelated include changes nothing.
    let (_unrelated, root) = repository();
    add_remote(&root, "origin", "https://github.com/acme/app");
    let identity = home().join("work-identity");
    fs::write(&identity, "[user]\n\temail = me@example.com\n").unwrap();
    global_config(&format!(
        "[includeIf \"gitdir:{}/\"]\n\tpath = {}\n",
        root.display(),
        identity.display()
    ));
    assert_eq!(
        problem(&context_of(&root).await),
        RepositoryContextProblem::Unspecified
    );
}

#[tokio::test]
async fn lists_remotes_without_their_urls_and_counts_unsupported_ones() {
    let _guard = serialized().await;
    let (_directory, root) = repository();
    add_remote(
        &root,
        "origin",
        "https://x-access-token:ghs_SECRET123@github.com/acme/app.git",
    );
    add_remote(&root, "alt", "https://github.com:8443/acme/app");
    add_remote(&root, "sub", "https://gitlab.com/group/sub/app");
    add_remote(
        &root,
        "ssh-default",
        "ssh://git@ghe.example.com:22/acme/app.git",
    );
    add_remote(
        &root,
        "ssh-custom",
        "ssh://git@ghe.example.com:7999/acme/app.git",
    );
    for index in 0..4 {
        add_remote(
            &root,
            &format!("extra{index}"),
            &format!("https://github.com/o/r{index}"),
        );
    }
    let context = context_of(&root).await;
    let text = format!("{context:?}");
    for secret in ["SECRET123", "x-access-token", "8443", "7999", "gitlab"] {
        assert!(!text.contains(secret), "{secret} leaked: {text}");
    }
    // Nine remotes: eight considered, and the three unsupported among them counted.
    assert!(context.truncated_remotes);
    assert_eq!(context.unsupported_remote_count, 3);
    let names: Vec<_> = context
        .remotes
        .iter()
        .map(|remote| remote.name.as_str())
        .collect();
    assert!(
        names.contains(&"origin") && names.contains(&"ssh-default"),
        "{names:?}"
    );
}
