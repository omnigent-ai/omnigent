use super::*;

fn identity(host: &str, owner: &str, repo: &str) -> Option<RepositoryIdentity> {
    Some(RepositoryIdentity {
        host: host.into(),
        owner: owner.into(),
        repo: repo.into(),
    })
}

#[test]
fn sanitizes_supported_remote_urls_to_host_owner_and_repo() {
    let expected = identity("github.com", "acme", "app");
    for url in [
        "https://github.com/acme/app",
        "https://github.com/acme/app.git",
        "https://github.com/acme/app/",
        "https://GitHub.com/acme/app.git",
        "https://user:token@github.com/acme/app.git",
        "https://github.com:443/acme/app",
        "https://github.com/acme/app?ref=1#x",
        "ssh://git@github.com/acme/app.git",
        "ssh://git@github.com:22/acme/app.git",
        "git@github.com:acme/app.git",
        "github.com:acme/app",
    ] {
        assert_eq!(sanitize_remote_url(url), expected, "{url}");
    }
}

#[test]
fn refuses_remote_urls_it_cant_map_to_one_repository() {
    for url in [
        "",
        "https://github.com:8443/acme/app",
        "ssh://git@ghe.example.com:7999/acme/app.git",
        "http://github.com/acme/app",
        "git://github.com/acme/app",
        "file:///srv/git/app.git",
        "/srv/git/app.git",
        "../app",
        "C:/repos/app",
        "https://gitlab.com/group/subgroup/app",
        "https://dev.azure.com/org/project/_git/app",
        "https://github.com/acme",
        "https://github.com/../app",
        "https://github.com/acme/app name",
        "https://-bad.example/acme/app",
        "git@github.com:/acme/app",
    ] {
        assert_eq!(sanitize_remote_url(url), None, "{url}");
    }
}

#[test]
fn never_returns_credentials_from_a_url() {
    let sanitized =
        sanitize_remote_url("https://x-access-token:ghs_SECRET@github.com/acme/app.git").unwrap();
    let text = format!("{sanitized:?}");
    assert!(
        !text.contains("SECRET") && !text.contains("x-access-token"),
        "{text}"
    );
}

const REPO_CONFIG: &str = "file:/repo/.git/config";

fn snapshot(entries: &[(&str, &str, &str)]) -> ConfigSnapshot {
    let mut bytes = Vec::new();
    for (origin, key, value) in entries {
        bytes.extend_from_slice(origin.as_bytes());
        bytes.push(0);
        bytes.extend_from_slice(key.as_bytes());
        bytes.push(b'\n');
        bytes.extend_from_slice(value.as_bytes());
        bytes.push(0);
    }
    ConfigSnapshot::parse(&bytes).unwrap()
}

fn local(entries: &[(&str, &str)]) -> ConfigSnapshot {
    let entries: Vec<_> = entries
        .iter()
        .map(|(key, value)| (REPO_CONFIG, *key, *value))
        .collect();
    snapshot(&entries)
}

fn origin_fetch(name: &str) -> String {
    format!("+refs/heads/*:refs/remotes/{name}/*")
}

fn git(push: Option<&str>) -> GitPushFields {
    GitPushFields {
        push: push.map(str::to_string),
        ..GitPushFields::default()
    }
}

#[test]
fn parses_remote_names_with_dots_and_slashes_and_valueless_keys() {
    let mut bytes =
        b"file:/repo/.git/config\0remote.team/fork.v2.url\nhttps://github.com/team/fork\0".to_vec();
    bytes.extend_from_slice(b"file:/repo/.git/config\0branch.main.rebase\0");
    let config = ConfigSnapshot::parse(&bytes).unwrap();
    assert_eq!(config.remote_names(), vec!["team/fork.v2"]);
    assert_eq!(config.entries[1].value, None);
}

#[test]
fn defers_url_rewrites_and_remotes_from_other_files_but_not_push_default() {
    let files = [PathBuf::from("/repo/.git/config")];
    let working = Path::new("/repo");
    let deferred = |entries: &[(&str, &str, &str)]| snapshot(entries).is_deferred(&files, working);
    assert!(!deferred(&[(
        REPO_CONFIG,
        "remote.origin.url",
        "https://github.com/a/b"
    )]));
    assert!(deferred(&[(
        "file:/home/u/.gitconfig",
        "url.git@github.com:.insteadof",
        "https://github.com/"
    )]));
    assert!(deferred(&[(REPO_CONFIG, "url.x.pushinsteadof", "y")]));
    assert!(deferred(&[(
        "file:/home/u/.gitconfig-work",
        "remote.origin.url",
        "https://github.com/a/b"
    )]));
    assert!(deferred(&[(
        "command line:",
        "branch.main.remote",
        "origin"
    )]));
    // A global pushDefault only picks a remote, which must be the repository's own.
    assert!(!deferred(&[(
        "file:/home/u/.gitconfig",
        "remote.pushdefault",
        "fork"
    )]));
    assert!(!deferred(&[(
        "file:/home/u/.gitconfig",
        "push.default",
        "current"
    )]));
    // A relative origin is resolved against the working directory.
    assert!(!deferred(&[(
        "file:.git/config",
        "remote.origin.url",
        "https://github.com/a/b"
    )]));
}

#[test]
fn pushes_the_current_branch_to_the_same_name_on_its_remote() {
    let config = local(&[
        ("remote.origin.url", "https://github.com/acme/app.git"),
        ("remote.origin.fetch", &origin_fetch("origin")),
        ("push.default", "current"),
    ]);
    let found = resolve_push(
        &config,
        "feature",
        &git(Some("refs/remotes/origin/feature")),
    )
    .unwrap()
    .unwrap();
    assert_eq!(found.remote_name, "origin");
    assert_eq!(found.branch, "feature");
    assert_eq!(
        found.identity,
        identity("github.com", "acme", "app").unwrap()
    );
    assert_eq!(
        found.tracking_ref.as_deref(),
        Some("refs/remotes/origin/feature")
    );
    assert!(found.fetches_from_push_repository);
}

#[test]
fn maps_a_renamed_fetch_destination_back_to_the_remote_branch() {
    let config = local(&[
        ("remote.origin.url", "https://github.com/acme/app"),
        (
            "remote.origin.fetch",
            "+refs/heads/*:refs/remotes/origin/cache/*",
        ),
        ("push.default", "current"),
    ]);
    let found = resolve_push(
        &config,
        "feature",
        &git(Some("refs/remotes/origin/cache/feature")),
    )
    .unwrap()
    .unwrap();
    assert_eq!(found.branch, "feature");
    assert_eq!(
        found.tracking_ref.as_deref(),
        Some("refs/remotes/origin/cache/feature")
    );
}

#[test]
fn follows_a_single_push_refspec_and_refuses_several() {
    let base = [
        ("remote.origin.url", "https://github.com/acme/app"),
        ("remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"),
    ];
    let one = local(&[
        base[0],
        base[1],
        (
            "remote.origin.push",
            "refs/heads/feature:refs/heads/review/feature",
        ),
    ]);
    let found = resolve_push(
        &one,
        "feature",
        &git(Some("refs/remotes/origin/review/feature")),
    )
    .unwrap()
    .unwrap();
    assert_eq!(found.branch, "review/feature");
    let glob = local(&[
        base[0],
        base[1],
        ("remote.origin.push", "refs/heads/*:refs/heads/team/*"),
    ]);
    assert_eq!(
        resolve_push(
            &glob,
            "feature",
            &git(Some("refs/remotes/origin/team/feature"))
        )
        .unwrap()
        .unwrap()
        .branch,
        "team/feature"
    );
    // A full source beside a short spelling of the same branch is ambiguous.
    let mixed = local(&[
        base[0],
        base[1],
        (
            "remote.origin.push",
            "refs/heads/feature:refs/heads/review-a",
        ),
        ("remote.origin.push", "heads/feature:refs/heads/review-b"),
    ]);
    assert_eq!(
        resolve_push(
            &mixed,
            "feature",
            &git(Some("refs/remotes/origin/review-a"))
        ),
        Err(PushUnresolved::Refspec)
    );
    // Git reports no push target for short or HEAD sources, so neither do we.
    for refspec in [
        "feature:review/feature",
        "heads/feature:refs/heads/review",
        "main:main",
        "HEAD:refs/heads/x",
        "refs/heads/feature:review",
    ] {
        let short = local(&[base[0], base[1], ("remote.origin.push", refspec)]);
        assert_eq!(
            resolve_push(&short, "feature", &git(None)),
            Err(PushUnresolved::Refspec),
            "{refspec}"
        );
    }
    let two = local(&[
        base[0],
        base[1],
        (
            "remote.origin.push",
            "refs/heads/feature:refs/heads/review-a",
        ),
        (
            "remote.origin.push",
            "refs/heads/feature:refs/heads/review-b",
        ),
    ]);
    assert_eq!(
        resolve_push(&two, "feature", &git(Some("refs/remotes/origin/review-a"))),
        Err(PushUnresolved::Refspec)
    );
    let negative = local(&[base[0], base[1], ("remote.origin.push", "^refs/heads/wip")]);
    assert_eq!(
        resolve_push(&negative, "feature", &git(None)),
        Err(PushUnresolved::Refspec)
    );
    let other = local(&[
        base[0],
        base[1],
        ("remote.origin.push", "refs/heads/main:refs/heads/main"),
    ]);
    assert_eq!(resolve_push(&other, "feature", &git(None)), Ok(None));
}

#[test]
fn refuses_ambiguous_or_negative_fetch_refspecs() {
    let ambiguous = local(&[
        ("remote.origin.url", "https://github.com/acme/app"),
        ("remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"),
        (
            "remote.origin.fetch",
            "+refs/heads/feature:refs/remotes/origin/pinned",
        ),
        ("push.default", "current"),
    ]);
    assert_eq!(
        resolve_push(
            &ambiguous,
            "feature",
            &git(Some("refs/remotes/origin/feature"))
        ),
        Err(PushUnresolved::Refspec)
    );
    let negative = local(&[
        ("remote.origin.url", "https://github.com/acme/app"),
        ("remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"),
        ("remote.origin.fetch", "^refs/heads/secret"),
        ("push.default", "current"),
    ]);
    assert_eq!(
        resolve_push(
            &negative,
            "feature",
            &git(Some("refs/remotes/origin/feature"))
        ),
        Err(PushUnresolved::Refspec)
    );
}

#[test]
fn withholds_evidence_when_git_disagrees() {
    let config = local(&[
        ("remote.origin.url", "https://github.com/acme/app"),
        ("remote.origin.fetch", &origin_fetch("origin")),
        ("push.default", "current"),
    ]);
    assert_eq!(
        resolve_push(&config, "feature", &git(Some("refs/remotes/origin/other"))),
        Err(PushUnresolved::Refspec)
    );
    // Git reports nothing for some pushes it makes, such as a triangular simple one.
    assert!(
        resolve_push(&config, "feature", &git(None))
            .unwrap()
            .is_some()
    );
    let mut wrong_remote = git(Some("refs/remotes/origin/feature"));
    wrong_remote.push_remote = Some("fork".into());
    assert_eq!(
        resolve_push(&config, "feature", &wrong_remote),
        Err(PushUnresolved::Refspec)
    );
}

#[test]
fn picks_the_push_remote_in_gits_order() {
    let remotes = [
        ("remote.origin.url", "https://github.com/acme/app"),
        ("remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"),
        ("remote.fork.url", "https://github.com/me/app"),
        ("remote.fork.fetch", "+refs/heads/*:refs/remotes/fork/*"),
        ("remote.team/fork.url", "https://github.com/team/app"),
        (
            "remote.team/fork.fetch",
            "+refs/heads/*:refs/remotes/team/fork/*",
        ),
        ("push.default", "current"),
    ];
    let with = |extra: &[(&str, &str)]| {
        let mut entries = remotes.to_vec();
        entries.extend_from_slice(extra);
        local(&entries)
    };
    let remote_of = |config: &ConfigSnapshot, push: &str| {
        resolve_push(config, "feature", &git(Some(push)))
            .unwrap()
            .unwrap()
            .remote_name
    };
    assert_eq!(
        remote_of(&with(&[]), "refs/remotes/origin/feature"),
        "origin"
    );
    assert_eq!(
        remote_of(
            &with(&[("branch.feature.remote", "fork")]),
            "refs/remotes/fork/feature"
        ),
        "fork"
    );
    assert_eq!(
        remote_of(
            &with(&[("remote.pushdefault", "fork")]),
            "refs/remotes/fork/feature"
        ),
        "fork"
    );
    assert_eq!(
        remote_of(
            &with(&[
                ("remote.pushdefault", "fork"),
                ("branch.feature.pushremote", "team/fork")
            ]),
            "refs/remotes/team/fork/feature"
        ),
        "team/fork"
    );
    // The sole remote stands in for origin.
    let sole = local(&[
        ("remote.fork.url", "https://github.com/me/app"),
        ("remote.fork.fetch", "+refs/heads/*:refs/remotes/fork/*"),
        ("push.default", "current"),
    ]);
    assert_eq!(remote_of(&sole, "refs/remotes/fork/feature"), "fork");
    // A remote that isn't the repository's own gives no evidence.
    assert_eq!(
        resolve_push(
            &with(&[("remote.pushdefault", "missing")]),
            "feature",
            &git(None)
        ),
        Ok(None)
    );
}

#[test]
fn applies_push_default_and_the_single_upstream_rules() {
    let origin = [
        ("remote.origin.url", "https://github.com/acme/app"),
        ("remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"),
    ];
    let with = |extra: &[(&str, &str)]| {
        let mut entries = origin.to_vec();
        entries.extend_from_slice(extra);
        local(&entries)
    };
    let feature = Some("refs/remotes/origin/feature");
    // Central simple with no upstream: Git refuses, so no evidence.
    assert_eq!(resolve_push(&with(&[]), "feature", &git(feature)), Ok(None));
    // Central simple whose upstream has the same name.
    let tracking = [
        ("branch.feature.remote", "origin"),
        ("branch.feature.merge", "refs/heads/feature"),
    ];
    assert!(
        resolve_push(&with(&tracking), "feature", &git(feature))
            .unwrap()
            .is_some()
    );
    // A merge without a remote isn't an upstream: Git refuses, so no evidence.
    let merge_only = [("branch.feature.merge", "refs/heads/feature")];
    assert_eq!(
        resolve_push(&with(&merge_only), "feature", &git(None)),
        Ok(None)
    );
    // Central simple tracking another branch: no evidence.
    let main = [
        ("branch.feature.remote", "origin"),
        ("branch.feature.merge", "refs/heads/main"),
    ];
    assert_eq!(resolve_push(&with(&main), "feature", &git(None)), Ok(None));
    // upstream pushes to the single upstream on its own remote.
    let mut upstream = main.to_vec();
    upstream.push(("push.default", "upstream"));
    let found = resolve_push(
        &with(&upstream),
        "feature",
        &git(Some("refs/remotes/origin/main")),
    )
    .unwrap()
    .unwrap();
    assert_eq!(found.branch, "main");
    // Two upstreams: Git refuses, so no evidence, though %(push) names one.
    let mut two = upstream.clone();
    two.push(("branch.feature.merge", "refs/heads/other"));
    assert_eq!(
        resolve_push(
            &with(&two),
            "feature",
            &git(Some("refs/remotes/origin/main"))
        ),
        Ok(None)
    );
    assert_eq!(
        resolve_push(
            &with(&[("push.default", "matching")]),
            "feature",
            &git(feature)
        ),
        Err(PushUnresolved::PushDefault)
    );
    assert_eq!(
        resolve_push(&with(&[("push.default", "nothing")]), "feature", &git(None)),
        Ok(None)
    );
    assert_eq!(
        resolve_push(
            &with(&[("push.default", "sideways")]),
            "feature",
            &git(None)
        ),
        Ok(None)
    );
    assert_eq!(
        resolve_push(
            &with(&[
                ("push.default", "current"),
                ("push.autosetupremote", "true")
            ]),
            "feature",
            &git(feature)
        ),
        Ok(None)
    );
}

#[test]
fn takes_the_push_repository_from_push_urls() {
    let base = [
        ("remote.origin.url", "https://github.com/upstream/app"),
        ("remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"),
        ("push.default", "current"),
    ];
    let with = |extra: &[(&str, &str)]| {
        let mut entries = base.to_vec();
        entries.extend_from_slice(extra);
        local(&entries)
    };
    let feature = git(Some("refs/remotes/origin/feature"));
    let found = resolve_push(
        &with(&[("remote.origin.pushurl", "git@github.com:fork/app.git")]),
        "feature",
        &feature,
    )
    .unwrap()
    .unwrap();
    assert_eq!(
        found.identity,
        identity("github.com", "fork", "app").unwrap()
    );
    assert!(!found.fetches_from_push_repository);
    let same = with(&[
        ("remote.origin.pushurl", "https://github.com/fork/app"),
        ("remote.origin.pushurl", "git@github.com:fork/app.git"),
    ]);
    assert!(resolve_push(&same, "feature", &feature).unwrap().is_some());
    let different = with(&[
        ("remote.origin.pushurl", "https://github.com/fork/app"),
        ("remote.origin.pushurl", "https://github.com/other/app"),
    ]);
    assert_eq!(
        resolve_push(&different, "feature", &feature),
        Err(PushUnresolved::PushUrl)
    );
    let port = with(&[("remote.origin.pushurl", "https://github.com:8443/fork/app")]);
    assert_eq!(
        resolve_push(&port, "feature", &feature),
        Err(PushUnresolved::PushUrl)
    );
}

#[test]
fn lists_supported_remotes_and_counts_the_rest() {
    let mut entries: Vec<(String, String)> = Vec::new();
    for index in 0..9 {
        entries.push((
            format!("remote.r{index}.url"),
            format!("https://github.com/o/r{index}"),
        ));
    }
    entries[1].1 = "https://gitlab.com/group/sub/repo".into();
    let borrowed: Vec<(&str, &str)> = entries
        .iter()
        .map(|(k, v)| (k.as_str(), v.as_str()))
        .collect();
    let (remotes, unsupported, truncated) = sanitized_remotes(&local(&borrowed));
    assert_eq!(remotes.len(), 7);
    assert_eq!(unsupported, 1);
    assert!(truncated);
    assert_eq!(remotes[0].name, "r0");
}

/// A fake Git whose configuration snapshot names `owner` "old" for the first
/// `old_reads` reads and "new" after, or a fresh value on every read.
fn changing_git(root: &Path, old_reads: Option<u32>) -> String {
    let counter = root.join("config-reads");
    let config = |owner: &str| {
        format!(
            r"file:{root}/.git/config\000remote.origin.url\nhttps://github.com/{owner}/app\000file:{root}/.git/config\000remote.origin.fetch\n+refs/heads/*:refs/remotes/origin/*\000file:{root}/.git/config\000push.default\ncurrent\000",
            root = root.display()
        )
    };
    let snapshot = match old_reads {
        Some(limit) => format!(
            r#"if [ "$n" -le {limit} ]; then printf '{old}'; else printf '{new}'; fi"#,
            old = config("old"),
            new = config("new"),
        ),
        None => format!(r#"printf '{}'"#, config("n$n").replace("n$n", "'\"n$n\"'")),
    };
    format!(
        r#"{discovery}case "$*" in
*config\ -z*) n=$(cat '{counter}' 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > '{counter}'; {snapshot}; exit 0;;
*symbolic-ref*) echo refs/heads/feature; exit 0;;
*for-each-ref*) printf 'refs/remotes/origin/feature\000\000refs/remotes/origin/feature'; exit 0;;
*rev-parse\ --verify*) echo {sha}; exit 0;;
esac
exit 2
"#,
        discovery = crate::git_runner::fake_discovery(root),
        counter = counter.display(),
        sha = "a".repeat(40),
    )
}

fn deadline() -> Instant {
    Instant::now() + std::time::Duration::from_secs(20)
}

#[tokio::test]
async fn rereads_when_push_configuration_changes_during_a_read() {
    let directory = tempfile::tempdir().unwrap();
    let root = std::fs::canonicalize(directory.path()).unwrap();
    // HEAD and the remote names stay put; only the push URL moves, once.
    crate::git_runner::fake_git(&changing_git(&root, Some(1)));
    let context = resolve(&root, deadline()).await.unwrap();
    let push = context.push.unwrap();
    assert_eq!(push.identity.owner, "new");
    assert_eq!(context.remotes[0].identity.owner, "new");
    assert_eq!(
        std::fs::read_to_string(root.join("config-reads"))
            .unwrap()
            .trim(),
        "4"
    );
}

#[tokio::test]
async fn declines_a_repository_that_keeps_changing() {
    let directory = tempfile::tempdir().unwrap();
    let root = std::fs::canonicalize(directory.path()).unwrap();
    crate::git_runner::fake_git(&changing_git(&root, None));
    assert_eq!(
        resolve(&root, deadline()).await,
        Err(ContextProblem::RepositoryChanging)
    );
}
