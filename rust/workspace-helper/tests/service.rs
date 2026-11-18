//! Each RPC end to end over a Unix socket.

mod common;

use std::{
    fs,
    os::unix::fs::{PermissionsExt, symlink},
};

use common::{canonical, collect, git, settled, start, start_binary, start_service, write};
use tonic::Code;
use workspace_helper::proto::{
    ComparisonKind, ContentEncoding, Entry, EntryType, FileSearchMode, GetCapabilitiesRequest,
    GetInfoRequest, ListDirectoryRequest, OpenWorkspaceRequest, ReadFileRequest,
    SearchFilesRequest, SearchOrder, SearchTextRequest, StatPathRequest,
};
use workspace_helper::{server::WorkspaceHelperService, text_search};

fn names(entries: &[Entry]) -> Vec<&str> {
    entries.iter().map(|entry| entry.name.as_str()).collect()
}

#[tokio::test(flavor = "multi_thread")]
async fn get_info_reports_protocol_and_capabilities() {
    let mut helper = start().await;
    let info = helper
        .client
        .get_info(GetInfoRequest {})
        .await
        .unwrap()
        .into_inner();
    assert_eq!(info.protocol_version, 1);
    assert_eq!(info.version, "dev");
    for capability in [
        "ListDirectory",
        "ReadFile",
        "SearchText",
        "search_files.fuzzy",
        "text_search_priority",
        "open_workspace.ahead_of_search",
    ] {
        assert!(
            info.capabilities.iter().any(|name| name == capability),
            "{capability}"
        );
    }
    // File lists refresh without watching the disk, and this helper serves
    // no terminals and stages no files.
    for absent in [
        "watch_changes",
        "pty",
        "InspectSession",
        "WriteAttachment",
        "ListStagedSessions",
        "ReleaseTerminal",
    ] {
        assert!(!info.capabilities.contains(&absent.to_string()), "{absent}");
    }
    // The workspace API lists the same.
    let listed = helper
        .client
        .get_capabilities(GetCapabilitiesRequest {})
        .await
        .unwrap()
        .into_inner();
    assert_eq!(listed.capabilities, info.capabilities);
}

#[tokio::test(flavor = "multi_thread")]
async fn open_workspace_validates_the_root_and_shares_state_for_one_root() {
    let mut helper = start().await;
    let open = |root: &str| OpenWorkspaceRequest {
        root: root.into(),
        ..Default::default()
    };
    let relative = helper
        .client
        .open_workspace(open("relative/path"))
        .await
        .unwrap_err();
    assert_eq!(relative.code(), Code::InvalidArgument);
    let missing = helper
        .client
        .open_workspace(open("/no/such/workspace"))
        .await
        .unwrap_err();
    assert_eq!(missing.code(), Code::NotFound);

    let directory = tempfile::tempdir().unwrap();
    let root = canonical(&directory);
    let first = helper
        .client
        .open_workspace(open(root.to_str().unwrap()))
        .await
        .unwrap()
        .into_inner();
    let second = helper.open(&root).await;
    assert_eq!(first.workspace_id, second);
    assert_eq!(first.root, root.to_str().unwrap());
    assert_eq!(first.repository_root, "");

    // Ids are random, so one from another helper names nothing here.
    assert_eq!(first.workspace_id.len(), 32);
    helper.client.pin(&"0".repeat(32));
    let unknown = helper
        .client
        .list_directory(ListDirectoryRequest::default())
        .await
        .unwrap_err();
    assert_eq!(unknown.code(), Code::NotFound);
    helper.client.pin("");
    let unpinned = helper
        .client
        .list_directory(ListDirectoryRequest::default())
        .await
        .unwrap_err();
    assert_eq!(unpinned.code(), Code::FailedPrecondition);
}

#[tokio::test(flavor = "multi_thread")]
async fn a_workspace_opened_ahead_of_a_search_outside_git_is_still_searched() {
    let directory = tempfile::tempdir().unwrap();
    let root = canonical(&directory);
    write(&root, "notes/plan.md", "");
    let mut helper = start().await;
    helper
        .client
        .open_workspace(OpenWorkspaceRequest {
            root: root.to_str().unwrap().into(),
            ahead_of_search: true,
        })
        .await
        .unwrap();
    // The walk an open ahead of a search left undone runs for the search.
    let paths = substring_paths(
        &mut helper,
        SearchFilesRequest {
            query: "plan".into(),
            ..Default::default()
        },
    )
    .await;
    assert_eq!(paths, ["notes/plan.md"]);
}

#[tokio::test(flavor = "multi_thread")]
async fn list_directory_describes_entries_like_the_runner() {
    let directory = tempfile::tempdir().unwrap();
    let root = canonical(&directory);
    write(&root, "b.txt", "four");
    write(&root, "a/inner.txt", "x");
    write(&root, ".hidden", "");
    symlink("a", root.join("link-to-dir")).unwrap();
    symlink("missing-target", root.join("broken")).unwrap();
    let mut helper = start().await;
    helper.open(&root).await;

    let listing = helper
        .client
        .list_directory(ListDirectoryRequest {
            ..Default::default()
        })
        .await
        .unwrap()
        .into_inner();
    assert_eq!(
        names(&listing.entries),
        vec![".hidden", "a", "b.txt", "broken", "link-to-dir"]
    );
    let entry = |name: &str| {
        listing
            .entries
            .iter()
            .find(|entry| entry.name == name)
            .unwrap()
    };
    assert_eq!(entry("b.txt").size, Some(4));
    assert_eq!(entry("b.txt").r#type, EntryType::File as i32);
    assert!(entry("b.txt").modify_time.is_some());
    assert_eq!(entry("a").r#type, EntryType::Directory as i32);
    assert_eq!(entry("a").size, None);
    // Kind follows the link; a broken link is a file without a size.
    assert_eq!(entry("link-to-dir").r#type, EntryType::Directory as i32);
    assert!(entry("link-to-dir").is_symlink);
    assert_eq!(entry("broken").r#type, EntryType::File as i32);
    assert_eq!(entry("broken").size, None);
    assert!(entry("broken").modify_time.is_some());

    let nested = helper
        .client
        .list_directory(ListDirectoryRequest {
            path: "./a/".into(),
            ..Default::default()
        })
        .await
        .unwrap()
        .into_inner();
    assert_eq!(nested.entries[0].path, "a/inner.txt");
}

#[tokio::test(flavor = "multi_thread")]
async fn list_directory_flags_or_hides_git_ignored_entries() {
    let fixture = common::ignore_fixture();
    let root = canonical(&fixture);
    let mut helper = start().await;
    helper.open(&root).await;
    let list = |path: &str, hide_ignored: bool| ListDirectoryRequest {
        path: path.into(),
        hide_ignored,
        ..Default::default()
    };

    let top = helper
        .client
        .list_directory(list("", false))
        .await
        .unwrap()
        .into_inner();
    let ignored: Vec<&str> = top
        .entries
        .iter()
        .filter(|entry| entry.ignored)
        .map(|entry| entry.name.as_str())
        .collect();
    assert_eq!(
        ignored,
        vec!["anchored.txt", "app.log", "build", "node_modules"]
    );
    // .git is listed, as the runner lists it, but is never ignored.
    assert!(
        top.entries
            .iter()
            .any(|entry| entry.name == ".git" && !entry.ignored)
    );

    let src = helper
        .client
        .list_directory(list("src", true))
        .await
        .unwrap()
        .into_inner();
    assert_eq!(
        names(&src.entries),
        vec![
            ".gitignore",
            "anchored.txt",
            "deep",
            "important.tmp",
            "main.rs"
        ]
    );

    // Everything beneath an ignored directory is ignored.
    let inside = helper
        .client
        .list_directory(list("node_modules/pkg", false))
        .await
        .unwrap()
        .into_inner();
    assert!(inside.entries.iter().all(|entry| entry.ignored));
}

#[tokio::test(flavor = "multi_thread")]
async fn list_directory_reports_single_child_directory_chains() {
    let directory = tempfile::tempdir().unwrap();
    let root = canonical(&directory);
    write(&root, "com/example/app/Main.java", "");
    write(&root, "split/one/x", "");
    write(&root, "split/two/y", "");
    fs::create_dir_all(root.join("empty")).unwrap();
    let mut helper = start().await;
    helper.open(&root).await;
    let listing = helper
        .client
        .list_directory(ListDirectoryRequest {
            collapse_single_child_directories: true,
            ..Default::default()
        })
        .await
        .unwrap()
        .into_inner();
    let leaf = |name: &str| {
        listing
            .entries
            .iter()
            .find(|entry| entry.name == name)
            .unwrap()
            .leaf_directory_path
            .clone()
    };
    assert_eq!(leaf("com"), "com/example/app");
    assert_eq!(leaf("split"), "");
    assert_eq!(leaf("empty"), "");

    let without = helper
        .client
        .list_directory(ListDirectoryRequest {
            ..Default::default()
        })
        .await
        .unwrap()
        .into_inner();
    assert!(
        without
            .entries
            .iter()
            .all(|entry| entry.leaf_directory_path.is_empty())
    );
}

/// `{name}-0 -> {name}-1 -> ... -> target`, `length` links in `root`.
fn link_chain(root: &std::path::Path, name: &str, length: usize, target: &std::path::Path) {
    for index in 0..length {
        let next = if index + 1 == length {
            target.to_path_buf()
        } else {
            format!("{name}-{}", index + 1).into()
        };
        symlink(next, root.join(format!("{name}-{index}"))).unwrap();
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn symlink_chains_that_lead_outside_are_refused_however_long() {
    let outside = tempfile::tempdir().unwrap();
    let outside_root = canonical(&outside);
    write(&outside_root, "secret.txt", "secret needle");
    let directory = tempfile::tempdir().unwrap();
    let root = canonical(&directory);
    write(&root, "inside.txt", "needle");
    // Longer than any symlink budget, and short enough to resolve.
    link_chain(&root, "long", 45, &outside_root);
    link_chain(&root, "short", 3, &outside_root);
    let mut helper = start().await;
    helper.open(&root).await;

    for chain in ["long-0", "short-0"] {
        let read = helper
            .client
            .read_file(ReadFileRequest {
                path: format!("{chain}/secret.txt"),
                maximum_bytes: 0,
            })
            .await;
        assert!(read.is_err(), "{chain}: read through the chain");
        let listing = helper
            .client
            .list_directory(ListDirectoryRequest {
                path: chain.into(),
                ..Default::default()
            })
            .await;
        assert!(listing.is_err(), "{chain}: listed through the chain");
        let stat = helper
            .client
            .stat_path(StatPathRequest {
                path: format!("{chain}/secret.txt"),
            })
            .await;
        assert!(stat.is_err(), "{chain}: described through the chain");
        let files = helper
            .client
            .search_files(SearchFilesRequest {
                query: "secret".into(),
                path: chain.into(),
                ..Default::default()
            })
            .await;
        assert!(files.is_err(), "{chain}: searched files through the chain");
        let text = helper
            .client
            .search_text(SearchTextRequest {
                query: "needle".into(),
                include_directories: vec![chain.into()],
                ..Default::default()
            })
            .await;
        assert!(text.is_err(), "{chain}: searched text through the chain");
    }

    // A whole-workspace search never follows the chains out either.
    let found = collect(
        helper
            .client
            .search_text(SearchTextRequest {
                query: "needle".into(),
                ..Default::default()
            })
            .await
            .unwrap()
            .into_inner(),
    )
    .await
    .unwrap();
    let paths: Vec<&str> = found
        .iter()
        .flat_map(|response| response.matches.iter().map(|found| found.path.as_str()))
        .collect();
    assert_eq!(paths, ["inside.txt"]);
}

#[tokio::test(flavor = "multi_thread")]
async fn paths_may_not_leave_the_workspace() {
    let outside = tempfile::tempdir().unwrap();
    write(&canonical(&outside), "secret.txt", "secret");
    let directory = tempfile::tempdir().unwrap();
    let root = canonical(&directory);
    symlink(canonical(&outside), root.join("escape")).unwrap();
    let mut helper = start().await;
    helper.open(&root).await;

    let read = |path: &str| ReadFileRequest {
        path: path.into(),
        maximum_bytes: 0,
    };
    let escaped = helper
        .client
        .read_file(read("escape/secret.txt"))
        .await
        .unwrap_err();
    // ListDirectory and ReadFile refuse as the host's reader does.
    assert_eq!(escaped.code(), Code::InvalidArgument);
    let traversal = helper
        .client
        .read_file(read("../secret.txt"))
        .await
        .unwrap_err();
    assert_eq!(traversal.code(), Code::InvalidArgument);
    let absolute = helper
        .client
        .read_file(read("/etc/hosts"))
        .await
        .unwrap_err();
    assert_eq!(absolute.code(), Code::InvalidArgument);
    let listing = helper
        .client
        .list_directory(ListDirectoryRequest {
            path: "escape".into(),
            ..Default::default()
        })
        .await
        .unwrap_err();
    assert_eq!(listing.code(), Code::InvalidArgument);
    let stat = helper
        .client
        .stat_path(StatPathRequest {
            path: "escape/secret.txt".into(),
        })
        .await
        .unwrap_err();
    assert_eq!(stat.code(), Code::PermissionDenied);
}

#[tokio::test(flavor = "multi_thread")]
async fn stat_path_describes_files_symlinks_and_missing_paths() {
    let directory = tempfile::tempdir().unwrap();
    let root = canonical(&directory);
    write(&root, "dir/file.txt", "hello");
    symlink("file.txt", root.join("dir/alias")).unwrap();
    let mut helper = start().await;
    helper.open(&root).await;
    let stat = |path: &str| StatPathRequest { path: path.into() };

    let file = helper
        .client
        .stat_path(stat("dir/file.txt"))
        .await
        .unwrap()
        .into_inner()
        .entry
        .unwrap();
    assert_eq!(
        (file.name.as_str(), file.path.as_str(), file.size),
        ("file.txt", "dir/file.txt", Some(5))
    );
    let alias = helper
        .client
        .stat_path(stat("dir/alias"))
        .await
        .unwrap()
        .into_inner()
        .entry
        .unwrap();
    assert!(alias.is_symlink);
    assert_eq!(alias.size, Some(5));
    let root_entry = helper
        .client
        .stat_path(stat(""))
        .await
        .unwrap()
        .into_inner()
        .entry
        .unwrap();
    assert_eq!(root_entry.r#type, EntryType::Directory as i32);
    let missing = helper
        .client
        .stat_path(stat("dir/missing"))
        .await
        .unwrap_err();
    assert_eq!(missing.code(), Code::NotFound);
}

#[tokio::test(flavor = "multi_thread")]
async fn read_file_streams_contents_and_reports_truncation() {
    let directory = tempfile::tempdir().unwrap();
    let root = canonical(&directory);
    let large: String = (0..200_000)
        .map(|index| char::from(b'a' + (index % 26) as u8))
        .collect();
    write(&root, "large.txt", &large);
    write(&root, "empty.txt", "");
    fs::create_dir(root.join("folder")).unwrap();
    let mut helper = start().await;
    helper.open(&root).await;
    let read = |path: &str, maximum_bytes: u64| ReadFileRequest {
        path: path.into(),
        maximum_bytes,
    };

    let chunks = collect(
        helper
            .client
            .read_file(read("large.txt", 0))
            .await
            .unwrap()
            .into_inner(),
    )
    .await
    .unwrap();
    // Metadata first, describing what the stream delivers, then the bytes.
    let metadata = chunks[0].metadata.clone().unwrap();
    assert_eq!(metadata.size, 200_000);
    assert!(!metadata.truncated && metadata.modify_time.is_some());
    assert_eq!(metadata.encoding, ContentEncoding::Utf8 as i32);
    assert!(chunks[0].data.is_empty() && chunks.len() > 2);
    assert!(chunks[1..].iter().all(|chunk| chunk.metadata.is_none()));
    let data: Vec<u8> = chunks.iter().flat_map(|chunk| chunk.data.clone()).collect();
    assert_eq!(data, large.as_bytes());

    let capped = collect(
        helper
            .client
            .read_file(read("large.txt", 1000))
            .await
            .unwrap()
            .into_inner(),
    )
    .await
    .unwrap();
    let data: Vec<u8> = capped.iter().flat_map(|chunk| chunk.data.clone()).collect();
    assert_eq!(data, &large.as_bytes()[..1000]);
    let metadata = capped[0].metadata.clone().unwrap();
    assert!(metadata.truncated && metadata.size == 1000);

    let exact = collect(
        helper
            .client
            .read_file(read("large.txt", 200_000))
            .await
            .unwrap()
            .into_inner(),
    )
    .await
    .unwrap();
    assert!(!exact[0].metadata.clone().unwrap().truncated);

    let empty = collect(
        helper
            .client
            .read_file(read("empty.txt", 0))
            .await
            .unwrap()
            .into_inner(),
    )
    .await
    .unwrap();
    assert_eq!(empty.len(), 1);
    assert_eq!(empty[0].metadata.clone().unwrap().size, 0);

    let folder = helper
        .client
        .read_file(read("folder", 0))
        .await
        .unwrap_err();
    // As the host's reader answers: only regular files are read.
    assert_eq!(folder.code(), Code::NotFound);
    let missing = helper
        .client
        .read_file(read("missing", 0))
        .await
        .unwrap_err();
    assert_eq!(missing.code(), Code::NotFound);
}

#[tokio::test(flavor = "multi_thread")]
async fn search_files_finds_fuzzy_and_substring_matches_without_ignored_paths() {
    let fixture = common::ignore_fixture();
    let root = canonical(&fixture);
    write(&root, "web/src/shell/FolderTree.tsx", "");
    let mut helper = start().await;
    helper.open(&root).await;
    let search = |query: &str, mode: FileSearchMode, include_ignored: bool| SearchFilesRequest {
        query: query.into(),
        mode: mode as i32,
        include_ignored,
        ..Default::default()
    };

    let fuzzy = settled(&mut helper, search("fldtree", FileSearchMode::Fuzzy, false)).await;
    let first = fuzzy.results[0].entry.as_ref().unwrap();
    assert_eq!(first.path, "web/src/shell/FolderTree.tsx");
    assert!(!fuzzy.results[0].matched_indices.is_empty());
    assert!(fuzzy.indexed_paths > 0);

    let index_js = settled(
        &mut helper,
        search("index.js", FileSearchMode::Substring, false),
    )
    .await;
    assert!(index_js.results.is_empty(), "node_modules is ignored");
    let with_ignored = settled(
        &mut helper,
        search("index.js", FileSearchMode::Substring, true),
    )
    .await;
    let found: Vec<(&str, bool)> = with_ignored
        .results
        .iter()
        .map(|result| result.entry.as_ref().unwrap())
        .map(|entry| (entry.path.as_str(), entry.ignored))
        .collect();
    assert_eq!(
        found,
        vec![
            ("node_modules/pkg/index.js", true),
            ("web/node_modules/pkg/index.js", true)
        ]
    );

    // Substring results include directories and are scoped to `path`. A
    // directory holding only ignored files, as `src/deep/generated` does, has
    // nothing listed beneath it and isn't a result.
    write(&root, "src/deep/kept.rs", "");
    let scoped = settled(
        &mut helper,
        SearchFilesRequest {
            path: "src".into(),
            retry_untracked: true,
            ..search("deep", FileSearchMode::Substring, false)
        },
    )
    .await;
    let entries: Vec<(&str, i32)> = scoped
        .results
        .iter()
        .map(|result| result.entry.as_ref().unwrap())
        .map(|entry| (entry.path.as_str(), entry.r#type))
        .collect();
    assert_eq!(
        entries,
        vec![
            ("deep", EntryType::Directory as i32),
            ("deep/kept.rs", EntryType::File as i32)
        ]
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn search_text_streams_matches_and_honours_filters_and_limits() {
    let fixture = common::ignore_fixture();
    let root = canonical(&fixture);
    write(&root, "src/lib.rs", "fn needle() {}\nlet Needle = 1;\n");
    write(&root, "docs/notes.md", "a needle here\n");
    write(&root, "build/generated.rs", "needle\n");
    let mut helper = start().await;
    helper.open(&root).await;
    let search = |query: &str| SearchTextRequest {
        query: query.into(),
        ..Default::default()
    };
    let run = |responses: Vec<workspace_helper::proto::SearchTextResponse>| {
        let last = responses.last().unwrap().clone();
        assert!(last.done);
        let mut matches: Vec<(String, u64)> = responses
            .into_iter()
            .flat_map(|response| response.matches)
            .map(|found| (found.path, found.line))
            .collect();
        matches.sort();
        (matches, last)
    };

    let all = collect(
        helper
            .client
            .search_text(search("needle"))
            .await
            .unwrap()
            .into_inner(),
    )
    .await
    .unwrap();
    let (matches, last) = run(all);
    assert_eq!(
        matches,
        vec![
            ("docs/notes.md".into(), 1),
            ("src/lib.rs".into(), 1),
            ("src/lib.rs".into(), 2)
        ]
    );
    assert!(!last.truncated);

    let sensitive = SearchTextRequest {
        case_sensitive: true,
        include: "*.rs".into(),
        ..search("Needle")
    };
    let (matches, _) = run(collect(
        helper
            .client
            .search_text(sensitive)
            .await
            .unwrap()
            .into_inner(),
    )
    .await
    .unwrap());
    assert_eq!(matches, vec![("src/lib.rs".into(), 2)]);

    let limited = SearchTextRequest {
        maximum_results: 1,
        ..search("needle")
    };
    let (matches, last) = run(collect(
        helper
            .client
            .search_text(limited)
            .await
            .unwrap()
            .into_inner(),
    )
    .await
    .unwrap());
    assert_eq!(matches.len(), 1);
    assert!(last.truncated);

    let ignored = SearchTextRequest {
        include_ignored: true,
        ..search("needle")
    };
    let (matches, _) = run(collect(
        helper
            .client
            .search_text(ignored)
            .await
            .unwrap()
            .into_inner(),
    )
    .await
    .unwrap());
    assert!(matches.contains(&("build/generated.rs".into(), 1)));

    let invalid = SearchTextRequest {
        regular_expression: true,
        ..search("(unclosed")
    };
    let error = collect(
        helper
            .client
            .search_text(invalid)
            .await
            .unwrap()
            .into_inner(),
    )
    .await
    .unwrap_err();
    assert_eq!(error.code(), Code::InvalidArgument);
    let blank = helper.client.search_text(search("   ")).await.unwrap_err();
    assert_eq!(blank.code(), Code::InvalidArgument);
}

async fn substring_paths(helper: &mut common::Helper, request: SearchFilesRequest) -> Vec<String> {
    settled(
        helper,
        SearchFilesRequest {
            mode: FileSearchMode::Substring as i32,
            ..request
        },
    )
    .await
    .results
    .into_iter()
    .map(|result| result.entry.unwrap().path)
    .collect()
}

#[tokio::test(flavor = "multi_thread")]
async fn file_search_follows_git_ignore_rules_on_the_next_untracked_pass() {
    let fixture = common::ignore_fixture();
    let root = canonical(&fixture);
    write(&root, "sub/visible.txt", "");
    write(&root, "sub/later.txt", "");
    let excludes = root.join(".git/user-excludes");
    fs::write(&excludes, "").unwrap();
    common::git(
        &root,
        &["config", "core.excludesFile", excludes.to_str().unwrap()],
    );
    let mut helper = start().await;
    // A subdirectory workspace: the repository's .gitignore is outside it.
    helper.open(&root.join("sub")).await;
    let search = |query: &str, retry_untracked: bool| SearchFilesRequest {
        query: query.into(),
        retry_untracked,
        ..Default::default()
    };
    assert_eq!(
        substring_paths(&mut helper, search("visible", false)).await,
        vec!["visible.txt"]
    );
    assert_eq!(
        substring_paths(&mut helper, search("later", false)).await,
        vec!["later.txt"]
    );

    fs::write(&excludes, "visible.txt\n").unwrap();
    assert!(
        substring_paths(&mut helper, search("visible", true))
            .await
            .is_empty()
    );

    let gitignore = fs::read_to_string(root.join(".gitignore")).unwrap();
    write(&root, ".gitignore", &(gitignore + "sub/later.txt\n"));
    assert!(
        substring_paths(&mut helper, search("later", true))
            .await
            .is_empty()
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn file_search_scoped_to_a_symlinked_directory_searches_its_target() {
    let directory = tempfile::tempdir().unwrap();
    let root = canonical(&directory);
    write(&root, "inner/code.rs", "");
    symlink("inner", root.join("alias")).unwrap();
    let mut helper = start().await;
    helper.open(&root).await;
    for scope in ["inner", "alias"] {
        let found = substring_paths(
            &mut helper,
            SearchFilesRequest {
                query: "code".into(),
                path: scope.into(),
                ..Default::default()
            },
        )
        .await;
        assert_eq!(found, vec!["code.rs"], "scope {scope}");
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn searches_honour_include_and_exclude_directories() {
    let directory = tempfile::tempdir().unwrap();
    let root = canonical(&directory);
    for path in [
        "sql/core/needle.scala",
        "sql/legacy/needle.scala",
        "web/needle.ts",
        "docs/needle.md",
    ] {
        write(&root, path, "needle\n");
    }
    // An unreadable directory inside an exclude proves the walk never enters it.
    let locked = root.join("sql/legacy/locked");
    fs::create_dir_all(&locked).unwrap();
    fs::set_permissions(&locked, fs::Permissions::from_mode(0o000)).unwrap();
    let mut helper = start().await;
    helper.open(&root).await;
    let include_directories = vec!["sql".to_string(), "web".to_string()];
    let exclude_directories = vec!["sql/legacy".to_string()];

    let files = substring_paths(
        &mut helper,
        SearchFilesRequest {
            query: "needle".into(),
            include_directories: include_directories.clone(),
            exclude_directories: exclude_directories.clone(),
            ..Default::default()
        },
    )
    .await;
    assert_eq!(files, vec!["sql/core/needle.scala", "web/needle.ts"]);

    let responses = collect(
        helper
            .client
            .search_text(SearchTextRequest {
                query: "needle".into(),
                include_directories,
                exclude_directories,
                ..Default::default()
            })
            .await
            .unwrap()
            .into_inner(),
    )
    .await
    .unwrap();
    let last = responses.last().unwrap().clone();
    let mut paths: Vec<String> = responses
        .into_iter()
        .flat_map(|response| response.matches)
        .map(|found| found.path)
        .collect();
    paths.sort();
    assert_eq!(paths, vec!["sql/core/needle.scala", "web/needle.ts"]);
    assert!(
        !last.truncated && last.warning.is_empty(),
        "the excluded subtree was entered: {last:?}"
    );

    let missing = helper
        .client
        .search_files(SearchFilesRequest {
            query: "needle".into(),
            include_directories: vec!["no-such-dir".into()],
            ..Default::default()
        })
        .await
        .unwrap_err();
    assert_eq!(missing.code(), Code::NotFound);
    fs::set_permissions(&locked, fs::Permissions::from_mode(0o755)).unwrap();
}

async fn text_matches(
    helper: &mut common::Helper,
    request: SearchTextRequest,
) -> (Vec<(String, u64)>, bool) {
    let responses = collect(
        helper
            .client
            .search_text(request)
            .await
            .unwrap()
            .into_inner(),
    )
    .await
    .unwrap();
    let truncated = responses.last().unwrap().truncated;
    let mut matches: Vec<(String, u64)> = responses
        .into_iter()
        .flat_map(|response| response.matches)
        .map(|found| (found.path, found.line))
        .collect();
    matches.sort();
    (matches, truncated)
}

#[tokio::test(flavor = "multi_thread")]
async fn overlapping_include_directories_search_each_file_once() {
    let directory = tempfile::tempdir().unwrap();
    let root = canonical(&directory);
    write(&root, "src/sub/file.txt", "needle\n");
    symlink("src", root.join("alias")).unwrap();
    let mut helper = start().await;
    helper.open(&root).await;
    for includes in [
        vec!["src", "src/sub"],
        vec!["src/sub", "src"],
        vec!["src", "src"],
        vec!["src", "alias"],
    ] {
        let request = |maximum_results: u32| SearchTextRequest {
            query: "needle".into(),
            maximum_results,
            include_directories: includes.iter().map(|path| path.to_string()).collect(),
            ..Default::default()
        };
        let (matches, truncated) = text_matches(&mut helper, request(0)).await;
        assert_eq!(
            matches,
            vec![("src/sub/file.txt".into(), 1)],
            "{includes:?}"
        );
        assert!(!truncated);
        let (matches, truncated) = text_matches(&mut helper, request(1)).await;
        assert_eq!(matches.len(), 1);
        assert!(
            !truncated,
            "a duplicate match consumed the limit for {includes:?}"
        );
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn include_directories_in_different_repositories_anchor_global_rules_in_any_order() {
    let fixture = common::ignore_fixture();
    let root = canonical(&fixture);
    for path in ["docs/visible.txt", "nested/visible.txt", "visible.txt"] {
        write(&root, path, "needle\n");
    }
    let excludes = tempfile::NamedTempFile::new().unwrap();
    fs::write(excludes.path(), "/visible.txt\n").unwrap();
    let mut helper = common::start_with_global_excludes(Some(excludes.path().to_path_buf())).await;
    helper.open(&root).await;
    // Git: "/visible.txt" ignores nested/visible.txt (nested's top) but not
    // docs/visible.txt. "docs" sorts before "nested", so it walks first.
    for (includes, expected) in [
        (vec!["nested"], vec![]),
        (vec!["docs", "nested"], vec!["docs/visible.txt"]),
        (vec!["nested", "docs"], vec!["docs/visible.txt"]),
    ] {
        let include_directories: Vec<String> =
            includes.iter().map(|path| path.to_string()).collect();
        let (matches, _) = text_matches(
            &mut helper,
            SearchTextRequest {
                query: "needle".into(),
                include_directories: include_directories.clone(),
                ..Default::default()
            },
        )
        .await;
        let paths: Vec<&str> = matches.iter().map(|(path, _)| path.as_str()).collect();
        assert_eq!(paths, expected, "text search with includes {includes:?}");

        let files = substring_paths(
            &mut helper,
            SearchFilesRequest {
                query: "visible".into(),
                include_directories,
                ..Default::default()
            },
        )
        .await;
        assert_eq!(files, expected, "file search with includes {includes:?}");
    }
}

/// The paths of `request`'s matches in the order they arrived, and the last response.
async fn arrival_order(
    helper: &mut common::Helper,
    request: SearchTextRequest,
) -> (Vec<String>, workspace_helper::proto::SearchTextResponse) {
    let responses = collect(
        helper
            .client
            .search_text(request)
            .await
            .unwrap()
            .into_inner(),
    )
    .await
    .unwrap();
    let last = responses.last().unwrap().clone();
    assert!(last.done);
    let order = responses
        .into_iter()
        .flat_map(|response| response.matches)
        .map(|found| found.path)
        .collect();
    (order, last)
}

#[tokio::test(flavor = "multi_thread")]
async fn a_priority_search_streams_open_files_first_then_their_folders_then_changes() {
    let directory = tempfile::tempdir().unwrap();
    let root = canonical(&directory);
    git(&root, &["init", "-q"]);
    git(&root, &["config", "user.email", "test@example.com"]);
    git(&root, &["config", "user.name", "Test"]);
    for file in [
        "a/one.txt",
        "z/open.txt",
        "z/near.txt",
        "m/edited.txt",
        "top.txt",
    ] {
        write(&root, file, "needle\n");
    }
    git(&root, &["add", "-A"]);
    git(&root, &["commit", "-q", "-m", "files"]);
    write(&root, "m/edited.txt", "needle\nmore\n");
    // Git's answer, however slow, is what orders the changed pass.
    let mut helper = start_service(
        WorkspaceHelperService::new(None).with_priority_git_wait(text_search::TIME_LIMIT),
    )
    .await;
    helper.open(&root).await;

    let (order, last) = arrival_order(
        &mut helper,
        SearchTextRequest {
            query: "needle".into(),
            order: SearchOrder::Priority as i32,
            focused_paths: vec!["z/open.txt".into(), "../outside".into()],
            ..Default::default()
        },
    )
    .await;
    assert_eq!(
        &order[..3],
        &["z/open.txt", "z/near.txt", "m/edited.txt"],
        "{order:?}"
    );
    let mut rest = order[3..].to_vec();
    rest.sort();
    assert_eq!(rest, vec!["a/one.txt", "top.txt"]);
    assert!(!last.truncated, "{last:?}");

    // A walk request is today's search: the same matches, with the cap applied.
    let (walked, last) = arrival_order(
        &mut helper,
        SearchTextRequest {
            query: "needle".into(),
            focused_paths: vec!["z/open.txt".into()],
            ..Default::default()
        },
    )
    .await;
    let mut walked_sorted = walked.clone();
    walked_sorted.sort();
    let mut prioritized_sorted = order.clone();
    prioritized_sorted.sort();
    assert_eq!(walked_sorted, prioritized_sorted);
    assert!(!last.truncated);
    let (capped, last) = arrival_order(
        &mut helper,
        SearchTextRequest {
            query: "needle".into(),
            maximum_results: 2,
            ..Default::default()
        },
    )
    .await;
    assert_eq!(capped.len(), 2);
    assert!(last.truncated);
}

/// The first `git` on `PATH`, which a fake `git` hands its work to.
fn real_git() -> std::path::PathBuf {
    std::env::split_paths(&std::env::var_os("PATH").unwrap())
        .map(|directory| directory.join("git"))
        .find(|path| {
            fs::metadata(path).is_ok_and(|metadata| {
                metadata.is_file() && metadata.permissions().mode() & 0o111 != 0
            })
        })
        .expect("git is on PATH")
}

/// Lets every `git` the fake holds go on dropping, as on a failing test. A
/// fake that is already gone leaves no reader, which is fine.
struct GitGate(std::path::PathBuf);

impl Drop for GitGate {
    fn drop(&mut self) {
        use std::os::unix::fs::OpenOptionsExt;
        if let Ok(mut gate) = fs::OpenOptions::new()
            .write(true)
            .custom_flags(libc::O_NONBLOCK)
            .open(&self.0)
        {
            let _ = std::io::Write::write_all(&mut gate, b"\n");
        }
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn a_priority_search_stops_waiting_for_a_git_that_never_answers() {
    let directory = tempfile::tempdir().unwrap();
    let root = canonical(&directory);
    git(&root, &["init", "-q"]);
    git(&root, &["config", "user.email", "test@example.com"]);
    git(&root, &["config", "user.name", "Test"]);
    for file in ["a/one.txt", "z/open.txt", "m/edited.txt", "top.txt"] {
        write(&root, file, "needle\n");
    }
    git(&root, &["add", "-A"]);
    git(&root, &["commit", "-q", "-m", "files"]);
    write(&root, "m/edited.txt", "needle\nmore\n");

    // Only the helper's `git` is the fake: it logs each call, and once `hold`
    // exists, it never answers.
    let fake = tempfile::tempdir().unwrap();
    let (bin, home) = (fake.path().join("bin"), fake.path().join("home"));
    fs::create_dir_all(&bin).unwrap();
    fs::create_dir_all(&home).unwrap();
    let (calls, hold, gate) = (
        fake.path().join("calls"),
        fake.path().join("hold"),
        fake.path().join("gate"),
    );
    let gate_path = std::ffi::CString::new(gate.to_str().unwrap()).unwrap();
    // SAFETY: a valid, NUL-terminated path.
    assert_eq!(unsafe { libc::mkfifo(gate_path.as_ptr(), 0o600) }, 0);
    let script = bin.join("git");
    fs::write(
        &script,
        format!(
            "#!/bin/sh\necho \"$*\" >> '{}'\nif [ -e '{}' ]; then\n  read _ < '{}'\n  exit 1\nfi\nexec '{}' \"$@\"\n",
            calls.display(),
            hold.display(),
            gate.display(),
            real_git().display()
        ),
    )
    .unwrap();
    fs::set_permissions(&script, fs::Permissions::from_mode(0o755)).unwrap();
    let path = std::env::join_paths(
        std::iter::once(bin.clone())
            .chain(std::env::split_paths(&std::env::var_os("PATH").unwrap())),
    )
    .unwrap();
    let (mut helper, mut child) = start_binary(|command| {
        command
            .env("PATH", &path)
            .env("HOME", &home)
            .env("XDG_CONFIG_HOME", home.join(".config"));
    })
    .await;
    let _gate = GitGate(gate.clone());
    helper.open(&root).await;
    // Listing the files runs Git, so the helper's Git is the fake.
    settled(
        &mut helper,
        SearchFilesRequest {
            ..Default::default()
        },
    )
    .await;
    assert!(fs::metadata(&calls).is_ok_and(|calls| calls.len() > 0));
    fs::write(&hold, "").unwrap();

    let (mut order, last) = arrival_order(
        &mut helper,
        SearchTextRequest {
            query: "needle".into(),
            order: SearchOrder::Priority as i32,
            focused_paths: vec!["z/open.txt".into()],
            ..Default::default()
        },
    )
    .await;
    // Without the cap, the wait would run to the search's deadline, and the
    // rest of the workspace would go unsearched.
    assert!(!last.truncated && last.warning.is_empty(), "{last:?}");
    assert_eq!(order[0], "z/open.txt");
    order.sort();
    assert_eq!(
        order,
        vec!["a/one.txt", "m/edited.txt", "top.txt", "z/open.txt"]
    );
    drop(child.stdin.take());
    child.wait().unwrap();
}

#[tokio::test(flavor = "multi_thread")]
async fn a_priority_search_without_a_repository_or_open_files_searches_everything() {
    let directory = tempfile::tempdir().unwrap();
    let root = canonical(&directory);
    for file in ["a/one.txt", "b/two.txt"] {
        write(&root, file, "needle\n");
    }
    let mut helper = start().await;
    helper.open(&root).await;
    let (mut order, last) = arrival_order(
        &mut helper,
        SearchTextRequest {
            query: "needle".into(),
            order: SearchOrder::Priority as i32,
            ..Default::default()
        },
    )
    .await;
    order.sort();
    assert_eq!(order, vec!["a/one.txt", "b/two.txt"]);
    assert!(!last.truncated && last.warning.is_empty(), "{last:?}");
}

#[tokio::test(flavor = "multi_thread")]
async fn a_priority_search_never_follows_a_hint_out_of_the_workspace_or_through_a_symlink() {
    let directory = tempfile::tempdir().unwrap();
    let root = canonical(&directory);
    let elsewhere = tempfile::tempdir().unwrap();
    write(elsewhere.path(), "secret.txt", "needle\n");
    write(&root, "real/inside.txt", "needle\n");
    write(&root, "plain.txt", "needle\n");
    std::os::unix::fs::symlink(elsewhere.path(), root.join("outside")).unwrap();
    std::os::unix::fs::symlink(root.join("real"), root.join("alias")).unwrap();
    let mut helper = start().await;
    helper.open(&root).await;
    let (order, last) = arrival_order(
        &mut helper,
        SearchTextRequest {
            query: "needle".into(),
            order: SearchOrder::Priority as i32,
            focused_paths: vec![
                "outside/secret.txt".into(),
                "alias/inside.txt".into(),
                "../plain.txt".into(),
                "real/../plain.txt".into(),
            ],
            ..Default::default()
        },
    )
    .await;
    let mut sorted = order.clone();
    sorted.sort();
    // Only what a walk finds, which never follows links: no file from
    // elsewhere, and the aliased file once, by its real path.
    assert_eq!(sorted, vec!["plain.txt", "real/inside.txt"]);
    assert!(!last.truncated, "{last:?}");
}

#[tokio::test(flavor = "multi_thread")]
async fn changed_folders_follow_the_comparison_the_request_names() {
    let directory = tempfile::tempdir().unwrap();
    let root = canonical(&directory);
    git(&root, &["init", "-q", "-b", "main"]);
    git(&root, &["config", "user.email", "test@example.com"]);
    git(&root, &["config", "user.name", "Test"]);
    for file in ["branch/work.txt", "wip/draft.txt", "a/one.txt", "b/two.txt"] {
        write(&root, file, "needle\n");
    }
    git(&root, &["add", "-A"]);
    git(&root, &["commit", "-q", "-m", "base"]);
    // The default branch, as a fetched remote names it.
    let base = git(&root, &["rev-parse", "HEAD"]);
    git(
        &root,
        &["update-ref", "refs/remotes/origin/main", base.trim()],
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
    // Committed on the branch: many lines, so it's the heaviest change since the branch point.
    write(&root, "branch/work.txt", &"needle\n".repeat(20));
    git(&root, &["commit", "-q", "-am", "feature work"]);
    // Uncommitted: the only change since HEAD.
    write(&root, "wip/draft.txt", "needle\nmore\n");
    let mut helper = start().await;
    helper.open(&root).await;
    let first_path = |kind: ComparisonKind| SearchTextRequest {
        query: "needle".into(),
        order: SearchOrder::Priority as i32,
        changed_against: kind as i32,
        ..Default::default()
    };
    let (against_head, _) =
        arrival_order(&mut helper, first_path(ComparisonKind::Unspecified)).await;
    assert_eq!(against_head[0], "wip/draft.txt", "{against_head:?}");
    let (since_branch_point, _) =
        arrival_order(&mut helper, first_path(ComparisonKind::BranchPoint)).await;
    assert!(
        since_branch_point[..20]
            .iter()
            .all(|path| path == "branch/work.txt"),
        "{since_branch_point:?}"
    );
}
