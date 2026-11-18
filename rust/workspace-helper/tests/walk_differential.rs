//! The helper's walk skips exactly what the ignore crate's own walker
//! skipped, which is what rg skips, and in Git mode agrees with `git`.

mod common;

use std::{
    collections::{BTreeSet, HashSet},
    fs,
    path::{Path, PathBuf},
    sync::Mutex,
};

use common::{canonical, git, write};
use ignore::{WalkBuilder, WalkState, overrides::OverrideBuilder};
use workspace_helper::ignore_rules::IgnoreRules;
use workspace_helper::walk::{IgnoreMode, WalkOptions, walk_parallel};

/// The walk as the crate's walker did it, the reference for the helper's.
fn crate_walk(starts: &[PathBuf], options: &WalkOptions) -> BTreeSet<PathBuf> {
    let mode = options.mode.unwrap();
    let global = options
        .global_excludes
        .as_deref()
        .filter(|_| mode != IgnoreMode::None);
    let honour_git = mode != IgnoreMode::None;
    let reached = Mutex::new(BTreeSet::new());
    let mut pending: Vec<(Vec<PathBuf>, Option<PathBuf>)> = Vec::new();
    for start in starts {
        let repository = start
            .ancestors()
            .find(|directory| fs::symlink_metadata(directory.join(".git")).is_ok())
            .map(Path::to_path_buf);
        match pending.iter_mut().find(|(_, owner)| *owner == repository) {
            Some((group, _)) => group.push(start.clone()),
            None => pending.push((vec![start.clone()], repository)),
        }
    }
    while let Some((group, repository)) = pending.pop() {
        let mut builder = WalkBuilder::new(&group[0]);
        for start in &group[1..] {
            builder.add(start);
        }
        builder
            .hidden(false)
            .follow_links(false)
            .parents(honour_git)
            .require_git(true)
            .ignore(mode == IgnoreMode::Ripgrep)
            .git_ignore(honour_git)
            .git_exclude(honour_git)
            .git_global(false);
        if mode == IgnoreMode::Ripgrep {
            builder.add_custom_ignore_filename(".rgignore");
        }
        if let Some(overrides) = &options.overrides {
            builder.overrides(overrides.clone());
        }
        if let (Some(global), Some(repository)) = (global, &repository) {
            builder.current_dir(repository);
            let _ = builder.add_ignore(global);
        }
        builder.filter_entry(|entry| entry.file_name() != ".git");
        let nested = Mutex::new(Vec::new());
        let roots: HashSet<&Path> = group.iter().map(PathBuf::as_path).collect();
        builder.build_parallel().run(|| {
            let (reached, nested, roots) = (&reached, &nested, &roots);
            Box::new(move |entry| {
                let entry = entry.unwrap();
                reached.lock().unwrap().insert(entry.path().to_path_buf());
                if global.is_some()
                    && entry.file_type().is_some_and(|kind| kind.is_dir())
                    && !roots.contains(entry.path())
                    && fs::symlink_metadata(entry.path().join(".git")).is_ok()
                {
                    nested.lock().unwrap().push(entry.path().to_path_buf());
                    return WalkState::Skip;
                }
                WalkState::Continue
            })
        });
        for directory in nested.into_inner().unwrap() {
            pending.push((vec![directory.clone()], Some(directory)));
        }
    }
    reached.into_inner().unwrap()
}

fn helper_walk(starts: &[PathBuf], options: &WalkOptions) -> BTreeSet<PathBuf> {
    let reached = Mutex::new(BTreeSet::new());
    walk_parallel(starts, options, || {
        let reached = &reached;
        Box::new(move |entry| {
            reached
                .lock()
                .unwrap()
                .insert(entry.unwrap().path().to_path_buf());
            WalkState::Continue
        })
    });
    reached.into_inner().unwrap()
}

/// Fails, naming the path, when anything above `top` could change a walk.
fn assert_clean_ancestry(top: &Path) {
    for ancestor in top.ancestors().skip(1) {
        for name in [".jj", ".ignore", ".rgignore", ".gitignore"] {
            let path = ancestor.join(name);
            assert!(
                !path.exists(),
                "fixture ancestry isn't clean: {}",
                path.display()
            );
        }
    }
    let inside_repository = std::process::Command::new("git")
        .args(["rev-parse", "--git-dir"])
        .current_dir(top.parent().unwrap())
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .output()
        .unwrap()
        .status
        .success();
    assert!(
        !inside_repository,
        "fixture ancestry is inside a repository: {}",
        top.display()
    );
}

/// A `.git` above `top` that git rejects, such as a stray empty `/tmp/.git`:
/// the crate's walker took it for a repository, so it can't be the reference.
fn rejected_marker_above(top: &Path) -> Option<PathBuf> {
    top.ancestors()
        .skip(1)
        .map(|ancestor| ancestor.join(".git"))
        .find(|marker| fs::symlink_metadata(marker).is_ok())
}

/// Rules of every kind at several levels, in and out of repositories.
fn fixture(top: &Path) {
    let files = |directory: &Path| {
        for name in [
            "keep.txt",
            "plain.txt",
            "hidden.log",
            "a.global",
            "keep.global",
            "both.tmp",
        ] {
            write(directory, name, "");
        }
    };
    // Outside any repository: ripgrep's own files, both ways across levels.
    let loose = top.join("loose");
    write(&loose, ".ignore", "!keep.txt\nboth.tmp\n");
    write(&loose, ".rgignore", "!both.tmp\n");
    write(&loose, ".gitignore", "plain.txt\n");
    write(&loose, "child/.gitignore", "keep.txt\n");
    write(&loose, "child/.ignore", "both.tmp\nhidden.log\n");
    write(&loose, "child/grand/.rgignore", "hidden.log\n");
    write(&loose, "child/grand/.ignore", "!hidden.log\n");
    files(&loose);
    files(&loose.join("child"));
    files(&loose.join("child/grand"));

    // A repository with intervening rules, negation, and info/exclude.
    let repository = top.join("repository");
    fs::create_dir_all(&repository).unwrap();
    git(&repository, &["init", "-q"]);
    write(
        &repository,
        ".gitignore",
        "*.log\nbuild/\n!keep.txt\nplain.txt\n",
    );
    write(&repository, ".git/info/exclude", "both.tmp\n");
    write(&repository, "src/.gitignore", "!hidden.log\nkeep.txt\n");
    write(&repository, "src/deep/.ignore", "!plain.txt\n");
    write(&repository, "src/deep/.gitignore", "!keep.global\n");
    write(&repository, "build/pkg/output.txt", "");
    files(&repository);
    files(&repository.join("src"));
    files(&repository.join("src/deep"));
    // A valid nested repository, which anchors the global excludes at its top.
    let nested = repository.join("src/nested");
    fs::create_dir_all(&nested).unwrap();
    git(&nested, &["init", "-q"]);
    write(&nested, ".gitignore", "plain.txt\n");
    files(&nested);
    files(&nested.join("inner"));
    // A .jj boundary inside the repository: Git's files above stop here,
    // while the global excludes stay anchored at the repository.
    write(&repository, "jj/.jj/repo", "");
    write(&repository, "jj/.gitignore", "keep.txt\n");
    files(&repository.join("jj"));
    files(&repository.join("jj/inner"));
    // A linked worktree reads the main repository's info/exclude.
    git(
        &repository,
        &[
            "-c",
            "user.name=Example",
            "-c",
            "user.email=example@example.com",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "initial",
        ],
    );
    git(&repository, &["worktree", "add", "-q", "../worktree"]);
    files(&top.join("worktree"));

    // Outside a repository: a .jj directory alone, then .jj beside .git.
    write(top, "jj-only/.jj/repo", "");
    write(top, "jj-only/.gitignore", "hidden.log\n");
    files(&top.join("jj-only"));
    files(&top.join("jj-only/inner"));
    // Rules above a repository don't reach into it.
    write(top, "above/.gitignore", "*.txt\n");
    let below = top.join("above/repository");
    fs::create_dir_all(&below).unwrap();
    git(&below, &["init", "-q"]);
    files(&below);
}

/// `.git` markers git rejects, which the crate's walker took for repositories.
fn invalid_markers(top: &Path) {
    let repository = top.join("repository");
    // Nested: an empty .git file and a HEAD-only .git directory are plain
    // directories, so the repository's rules reach into them.
    write(&repository, "src/empty-marker/.git", "");
    write(&repository, "src/empty-marker/.gitignore", "keep.txt\n");
    write(
        &repository,
        "src/head-only/.git/HEAD",
        "ref: refs/heads/main\n",
    );
    for directory in ["src/empty-marker", "src/head-only"] {
        for name in ["keep.txt", "plain.txt", "hidden.log", "both.tmp"] {
            write(&repository.join(directory), name, "");
        }
    }
    // Above a start, outside any repository: its .gitignore doesn't apply.
    write(top, "invalid-top/.git", "");
    write(top, "invalid-top/.gitignore", "*.log\n");
    write(top, "invalid-top/sub/hidden.log", "");
    // Beside .jj, which is still a boundary for the walk.
    write(top, "jj-invalid-git/.jj/repo", "");
    write(top, "jj-invalid-git/.git", "");
    write(top, "jj-invalid-git/.gitignore", "hidden.log\n");
    write(top, "jj-invalid-git/hidden.log", "");
    write(top, "jj-invalid-git/plain.txt", "");
}

fn options(mode: IgnoreMode, global: Option<&Path>) -> WalkOptions {
    WalkOptions::new(mode, global)
}

#[test]
fn the_helper_walk_skips_what_the_crate_walker_skipped() {
    let directory = tempfile::tempdir().unwrap();
    let top = canonical(&directory).join("fixture");
    fs::create_dir(&top).unwrap();
    assert_clean_ancestry(&top);
    if let Some(marker) = rejected_marker_above(&top) {
        eprintln!(
            "skipping the crate comparison: {} is above the fixture",
            marker.display()
        );
        return;
    }
    fixture(&top);
    let global_file = top.join("global-excludes");
    write(
        &top,
        "global-excludes",
        "*.global\n!keep.global\nboth.tmp\n",
    );
    let starts: Vec<Vec<PathBuf>> = [
        vec![""],
        vec!["loose/child"],
        vec!["loose/child/grand", "repository/src/deep"],
        vec!["repository"],
        vec!["repository/src", "jj-only"],
        vec!["repository/src/nested/inner", "repository/jj/inner"],
        // A start under an ignored directory still yields its contents.
        vec!["repository/build/pkg"],
        vec!["worktree"],
        vec!["above"],
    ]
    .iter()
    .map(|group| group.iter().map(|start| top.join(start)).collect())
    .collect();
    let mut overrides = OverrideBuilder::new(&top);
    overrides.add("!*.tmp").unwrap();
    overrides.add("hidden.log").unwrap();
    let overrides = overrides.build().unwrap();
    let mut differences = Vec::new();
    for mode in [IgnoreMode::Git, IgnoreMode::Ripgrep, IgnoreMode::None] {
        for global in [None, Some(global_file.as_path())] {
            for with_overrides in [false, true] {
                let mut options = options(mode, global);
                if with_overrides {
                    options.overrides = Some(overrides.clone());
                }
                for group in &starts {
                    let expected = crate_walk(group, &options);
                    let actual = helper_walk(group, &options);
                    if expected != actual {
                        differences.push(format!(
                            "{mode:?} global={} overrides={with_overrides} {group:?}: \
                             only the crate {:?}, only the helper {:?}",
                            global.is_some(),
                            expected.difference(&actual).collect::<Vec<_>>(),
                            actual.difference(&expected).collect::<Vec<_>>(),
                        ));
                    }
                }
            }
        }
    }
    assert!(differences.is_empty(), "{differences:#?}");
}

#[test]
fn in_git_mode_the_helper_walk_lists_what_git_does() {
    let directory = tempfile::tempdir().unwrap();
    let top = canonical(&directory).join("fixture");
    fs::create_dir(&top).unwrap();
    assert_clean_ancestry(&top);
    fixture(&top);
    invalid_markers(&top);
    let global_file = top.join("global-excludes");
    write(
        &top,
        "global-excludes",
        "*.global\n!keep.global\nboth.tmp\n",
    );
    let excludes = format!("core.excludesFile={}", global_file.display());
    // Each repository on its own, without the nested ones git lists as one entry.
    for (repository, start, skip) in [
        ("repository", "repository", vec!["src/nested/", "jj/"]),
        ("repository", "repository/src/deep", vec![]),
        ("repository/src/nested", "repository/src/nested", vec![]),
        ("worktree", "worktree", vec![]),
        ("above/repository", "above/repository", vec![]),
    ] {
        let repository = top.join(repository);
        let walked: BTreeSet<String> = helper_walk(
            &[top.join(start)],
            &options(IgnoreMode::Git, Some(&global_file)),
        )
        .into_iter()
        .filter(|path| path.is_file())
        .map(|path| {
            path.strip_prefix(&repository)
                .unwrap()
                .to_string_lossy()
                .into_owned()
        })
        .filter(|path| !skip.iter().any(|prefix| path.starts_with(prefix)))
        .collect();
        let relative_start = top.join(start);
        let relative_start = relative_start.strip_prefix(&repository).unwrap();
        let listed: BTreeSet<String> = git(
            &repository,
            &[
                "-c",
                &excludes,
                "ls-files",
                "--others",
                "--exclude-standard",
                "--full-name",
                "--",
            ],
        )
        .lines()
        .filter(|path| !path.ends_with('/'))
        .filter(|path| Path::new(path).starts_with(relative_start))
        .filter(|path| !skip.iter().any(|prefix| path.starts_with(prefix)))
        .map(str::to_string)
        .collect();
        assert_eq!(walked, listed, "{}", repository.display());
    }
}

#[test]
fn markers_git_rejects_are_not_repositories() {
    let directory = tempfile::tempdir().unwrap();
    let top = canonical(&directory).join("fixture");
    fs::create_dir(&top).unwrap();
    assert_clean_ancestry(&top);
    fixture(&top);
    invalid_markers(&top);
    let walked = |start: &str| helper_walk(&[top.join(start)], &options(IgnoreMode::Git, None));
    // Outside any repository, the invalid marker's .gitignore ignores nothing.
    assert!(walked("invalid-top/sub").contains(&top.join("invalid-top/sub/hidden.log")));
    let rules = IgnoreRules::new(&top.join("invalid-top/sub"), None);
    assert!(!rules.is_ignored(&top.join("invalid-top/sub/hidden.log"), false));
    assert_eq!(rules.repository_root(&top.join("invalid-top/sub")), None);
    // .jj stays a boundary for the walk whatever its .git holds.
    let beside_jj = walked("jj-invalid-git");
    assert!(!beside_jj.contains(&top.join("jj-invalid-git/hidden.log")));
    assert!(beside_jj.contains(&top.join("jj-invalid-git/plain.txt")));
    // Nested inside a repository, single paths agree with git check-ignore.
    let repository = top.join("repository");
    let rules = IgnoreRules::new(&repository, None);
    for directory in ["src/empty-marker", "src/head-only"] {
        for name in ["keep.txt", "plain.txt", "hidden.log", "both.tmp"] {
            let path = format!("{directory}/{name}");
            let theirs = std::process::Command::new("git")
                .args(["check-ignore", "-q", "--", &path])
                .current_dir(&repository)
                .env("GIT_CONFIG_GLOBAL", "/dev/null")
                .env("GIT_CONFIG_NOSYSTEM", "1")
                .status()
                .unwrap()
                .success();
            assert_eq!(
                rules.is_ignored(&repository.join(&path), false),
                theirs,
                "{path}"
            );
        }
    }
}

const GITFILE_FILES: [&str; 4] = ["kept.txt", "excluded.txt", "negated.txt", "parent-rule.txt"];

fn commit_all(repository: &Path) {
    git(repository, &["add", "-A"]);
    git(
        repository,
        &[
            "-c",
            "user.name=Example",
            "-c",
            "user.email=example@example.com",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "initial",
        ],
    );
}

/// Repositories whose `.git` is a file naming their git directory by a
/// relative path: a submodule and a linked worktree made with relative paths.
/// Returns the submodule's and the worktree's tops.
fn gitfile_repositories(top: &Path) -> (PathBuf, PathBuf) {
    let source = top.join("source");
    write(&source, "tracked.txt", "");
    git(&source, &["init", "-q"]);
    commit_all(&source);
    let superproject = top.join("superproject");
    fs::create_dir_all(&superproject).unwrap();
    git(&superproject, &["init", "-q"]);
    git(
        &superproject,
        &[
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            "../source",
            "library",
        ],
    );
    // The superproject's rule names a file inside the submodule; git doesn't
    // apply it there.
    write(&superproject, ".gitignore", "library/parent-rule.txt\n");
    let submodule = superproject.join("library");
    write(
        &superproject,
        ".git/modules/library/info/exclude",
        "excluded.txt\nnegated.txt\n",
    );

    let main = top.join("main");
    fs::create_dir_all(&main).unwrap();
    git(&main, &["init", "-q"]);
    commit_all(&main);
    git(
        &main,
        &[
            "worktree",
            "add",
            "-q",
            "--relative-paths",
            "../relative-worktree",
        ],
    );
    let worktree = top.join("relative-worktree");
    write(&main, ".git/info/exclude", "excluded.txt\nnegated.txt\n");
    write(&main, "parent-rule.txt", "");

    for repository in [&submodule, &worktree] {
        // A .gitignore negation outranks info/exclude.
        write(repository, ".gitignore", "!negated.txt\n");
        for name in GITFILE_FILES {
            write(repository, name, "");
        }
    }
    (submodule, worktree)
}

#[test]
fn a_submodule_or_relative_worktree_honours_its_info_exclude() {
    let directory = tempfile::tempdir().unwrap();
    let top = canonical(&directory).join("fixture");
    fs::create_dir(&top).unwrap();
    assert_clean_ancestry(&top);
    let (submodule, worktree) = gitfile_repositories(&top);
    for (repository, start) in [
        (&submodule, submodule.clone()),
        (&submodule, top.join("superproject")),
        (&worktree, worktree.clone()),
    ] {
        let walked: BTreeSet<String> = helper_walk(
            std::slice::from_ref(&start),
            &options(IgnoreMode::Git, None),
        )
        .into_iter()
        .filter_map(|path| {
            let relative = path.strip_prefix(repository).ok()?;
            (path.is_file() && relative != Path::new(".git"))
                .then(|| relative.to_string_lossy().into_owned())
        })
        .collect();
        let listed: BTreeSet<String> = git(
            repository,
            // Tracked files and untracked ones git doesn't ignore.
            &[
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "--",
            ],
        )
        .lines()
        .map(str::to_string)
        .collect();
        let context = format!("{} from {}", repository.display(), start.display());
        assert_eq!(walked, listed, "{context}");
        assert!(!walked.contains("excluded.txt"), "{context}");
        assert!(walked.contains("negated.txt"), "{context}");
        assert!(walked.contains("parent-rule.txt"), "{context}");
    }
    for (workspace, repository) in [
        (&submodule, &submodule),
        (&top.join("superproject"), &submodule),
        (&worktree, &worktree),
    ] {
        let rules = IgnoreRules::new(workspace, None);
        for name in GITFILE_FILES {
            let theirs = std::process::Command::new("git")
                .args(["check-ignore", "-q", "--", name])
                .current_dir(repository)
                .env("GIT_CONFIG_GLOBAL", "/dev/null")
                .env("GIT_CONFIG_NOSYSTEM", "1")
                .status()
                .unwrap()
                .success();
            assert_eq!(
                rules.is_ignored(&repository.join(name), false),
                theirs,
                "{} in {}",
                name,
                workspace.display()
            );
        }
    }
}
