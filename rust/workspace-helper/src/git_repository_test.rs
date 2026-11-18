use std::{
    fs,
    os::unix::fs::{PermissionsExt, symlink},
    path::Path,
    process::Command,
    sync::mpsc,
    time::Duration,
};

use super::{discover, init_repository, repository_at};

fn git(directory: &Path, arguments: &[&str]) -> bool {
    Command::new("git")
        .args(arguments)
        .current_dir(directory)
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("GIT_CONFIG_NOSYSTEM", "1")
        // Git must not look above the case, as the helper doesn't.
        .env("GIT_CEILING_DIRECTORIES", directory.parent().unwrap())
        .env_remove("GIT_DIR")
        .output()
        .unwrap()
        .status
        .success()
}

fn write(path: &Path, contents: &str) {
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::write(path, contents).unwrap();
}

fn mkfifo(path: &Path) {
    let path = std::ffi::CString::new(path.as_os_str().as_encoded_bytes()).unwrap();
    // SAFETY: `path` is a valid NUL-terminated string for the duration of the call.
    assert_eq!(unsafe { libc::mkfifo(path.as_ptr(), 0o600) }, 0);
}

/// A repository as git checks for one (HEAD, objects and refs), made without running git. A
/// git wrapper may keep reading a repository after git returns, and would block on a FIFO there.
fn handmade_repository(directory: &Path) {
    write(&directory.join(".git/HEAD"), "ref: refs/heads/main\n");
    fs::create_dir_all(directory.join(".git/objects")).unwrap();
    fs::create_dir_all(directory.join(".git/refs")).unwrap();
}

/// A bare git directory at `path`, for `.git` files to name.
fn git_directory(path: &Path) {
    fs::create_dir_all(path).unwrap();
    assert!(git(path, &["init", "-q", "--bare"]));
}

#[test]
fn a_directory_is_a_repository_exactly_when_git_says_so() {
    let directory = tempfile::tempdir().unwrap();
    let top = directory.path().canonicalize().unwrap();
    let case = |name: &str| {
        let path = top.join("cases").join(name);
        fs::create_dir_all(&path).unwrap();
        path
    };
    let store = top.join("store.git");
    git_directory(&store);

    init_repository(&case("initialized"));
    let unborn_link = case("symbolic-link-head");
    init_repository(&unborn_link);
    fs::remove_file(unborn_link.join(".git/HEAD")).unwrap();
    symlink("refs/heads/not-yet", unborn_link.join(".git/HEAD")).unwrap();
    let detached = case("detached-head");
    init_repository(&detached);
    write(
        &detached.join(".git/HEAD"),
        &format!("{}\n", "a".repeat(40)),
    );
    write(
        &case("relative-gitdir").join(".git"),
        "gitdir: ../../store.git\n",
    );
    write(
        &case("absolute-gitdir").join(".git"),
        &format!("gitdir: {}\n", store.display()),
    );
    let main = case("main");
    init_repository(&main);
    assert!(git(
        &main,
        &[
            "-c",
            "user.name=Example",
            "-c",
            "user.email=example@example.com",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "initial"
        ]
    ));
    assert!(git(&main, &["worktree", "add", "-q", "../linked-worktree"]));

    write(&case("empty-file").join(".git"), "");
    fs::create_dir(case("empty-directory").join(".git")).unwrap();
    write(
        &case("head-only").join(".git/HEAD"),
        "ref: refs/heads/main\n",
    );
    write(
        &case("dangling-gitdir").join(".git"),
        "gitdir: ../missing.git\n",
    );
    write(
        &case("gitdir-without-space").join(".git"),
        "gitdir:../../store.git\n",
    );
    let relative_ref = case("ref-outside-refs");
    init_repository(&relative_ref);
    write(&relative_ref.join(".git/HEAD"), "ref: heads/main\n");
    let short_name = case("short-object-name");
    init_repository(&short_name);
    write(&short_name.join(".git/HEAD"), "abc123\n");
    let no_objects = case("no-objects");
    init_repository(&no_objects);
    fs::remove_dir_all(no_objects.join(".git/objects")).unwrap();
    let no_refs = case("no-refs");
    init_repository(&no_refs);
    fs::remove_dir_all(no_refs.join(".git/refs")).unwrap();
    let outside_link = case("symbolic-link-outside-refs");
    init_repository(&outside_link);
    fs::remove_file(outside_link.join(".git/HEAD")).unwrap();
    symlink("../HEAD-elsewhere", outside_link.join(".git/HEAD")).unwrap();

    // Git checks a .git file's type and size before reading it.
    mkfifo(&case("fifo-dot-git").join(".git"));
    write(
        &case("oversized-gitfile").join(".git"),
        &format!("gitdir: ../../store.git{}", "\n".repeat(1 << 20)),
    );
    // An existing commondir must be readable and not empty; a bare line ending
    // names the git directory itself.
    let empty_common = case("empty-commondir");
    init_repository(&empty_common);
    write(&empty_common.join(".git/commondir"), "");
    let directory_common = case("directory-commondir");
    init_repository(&directory_common);
    fs::create_dir(directory_common.join(".git/commondir")).unwrap();
    // A commondir past 1 MiB is read in full, so its path is the whole file.
    let oversized_common = case("oversized-commondir");
    init_repository(&oversized_common);
    let mut contents = b".".to_vec();
    contents.extend(std::iter::repeat_n(b'\n', (1 << 20) - 1));
    contents.extend(b"not-present");
    fs::write(oversized_common.join(".git/commondir"), contents).unwrap();
    let dangling_common = case("dangling-commondir");
    init_repository(&dangling_common);
    symlink("not-present", dangling_common.join(".git/commondir")).unwrap();
    let newline_common = case("newline-commondir");
    init_repository(&newline_common);
    write(&newline_common.join(".git/commondir"), "\n");
    // objects/ and refs/ need search permission, not to be directories.
    let unsearchable = case("unsearchable-refs");
    init_repository(&unsearchable);
    fs::set_permissions(
        unsearchable.join(".git/refs"),
        fs::Permissions::from_mode(0o600),
    )
    .unwrap();
    for name in ["objects", "refs"] {
        let executable = case(&format!("executable-file-{name}"));
        init_repository(&executable);
        fs::remove_dir_all(executable.join(".git").join(name)).unwrap();
        write(&executable.join(".git").join(name), "");
        fs::set_permissions(
            executable.join(".git").join(name),
            fs::Permissions::from_mode(0o700),
        )
        .unwrap();
    }
    // Git reads 255 bytes of HEAD and skips only space, tab, LF and CR.
    let long_head = case("head-past-255-bytes");
    init_repository(&long_head);
    write(
        &long_head.join(".git/HEAD"),
        &format!("ref:{}refs/heads/main\n", " ".repeat(260)),
    );
    let form_feed = case("head-form-feed");
    init_repository(&form_feed);
    write(&form_feed.join(".git/HEAD"), "ref:\x0crefs/heads/main\n");

    let (mut checked, mut valid) = (0, 0);
    let mut disagreements = Vec::new();
    for entry in fs::read_dir(top.join("cases")).unwrap() {
        let path = entry.unwrap().path();
        let ours = repository_at(&path).is_some();
        let theirs = git(&path, &["rev-parse", "--git-dir"]);
        if ours != theirs {
            disagreements.push(format!("{}: helper {ours}, git {theirs}", path.display()));
        }
        checked += 1;
        valid += usize::from(theirs);
    }
    fs::set_permissions(
        unsearchable.join(".git/refs"),
        fs::Permissions::from_mode(0o700),
    )
    .unwrap();
    assert_eq!((checked, valid), (29, 10));
    assert!(disagreements.is_empty(), "{disagreements:#?}");
}

#[test]
fn a_linked_worktree_uses_the_main_repository_as_its_common_directory() {
    let directory = tempfile::tempdir().unwrap();
    let top = directory.path().canonicalize().unwrap();
    let main = top.join("main");
    fs::create_dir(&main).unwrap();
    init_repository(&main);
    assert!(git(
        &main,
        &[
            "-c",
            "user.name=Example",
            "-c",
            "user.email=example@example.com",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "initial"
        ]
    ));
    assert!(git(&main, &["worktree", "add", "-q", "../linked"]));
    let linked = repository_at(&top.join("linked")).unwrap();
    assert_eq!(
        linked.common_directory.canonicalize().unwrap(),
        main.join(".git")
    );
    assert_ne!(
        linked.git_directory.canonicalize().unwrap(),
        main.join(".git")
    );
}

#[test]
fn discovery_passes_over_markers_git_rejects() {
    let directory = tempfile::tempdir().unwrap();
    let top = directory.path().canonicalize().unwrap();
    init_repository(&top);
    let nested = top.join("nested/deeper");
    fs::create_dir_all(&nested).unwrap();
    write(&top.join("nested/.git"), "");
    assert_eq!(discover(&nested).unwrap().root, top);
    assert_eq!(discover(&top.join("nested")).unwrap().root, top);
}

/// Set in the worker that runs the FIFO case: where it records that the case finished.
const FIFO_CASE_DONE: &str = "WORKSPACE_HELPER_FIFO_CASE_DONE";
/// Where the worker's only `git` records each time it's run.
const GIT_SENTINEL_LOG: &str = "WORKSPACE_HELPER_GIT_SENTINEL_LOG";

#[test]
fn a_fifo_where_git_reads_a_file_is_never_waited_on() {
    if let Some(done) = std::env::var_os(FIFO_CASE_DONE) {
        fifo_case();
        fs::write(done, "").unwrap();
        return;
    }
    // A git, or a wrapper it runs through, can block forever on a FIFO in a repository it read,
    // so the case runs in a worker of this binary whose only `git` records being run and fails.
    let scratch = tempfile::tempdir().unwrap();
    let bin = scratch.path().join("bin");
    write(
        &bin.join("git"),
        &format!("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"${GIT_SENTINEL_LOG}\"\nexit 1\n"),
    );
    fs::set_permissions(bin.join("git"), fs::Permissions::from_mode(0o755)).unwrap();
    let calls = scratch.path().join("git-calls");
    let done = scratch.path().join("done");
    let (_, module) = module_path!().split_once("::").unwrap();
    let output = Command::new(std::env::current_exe().unwrap())
        .args([
            "--exact",
            &format!("{module}::a_fifo_where_git_reads_a_file_is_never_waited_on"),
        ])
        .args(["--test-threads=1", "--nocapture"])
        .env(FIFO_CASE_DONE, &done)
        .env(GIT_SENTINEL_LOG, &calls)
        .env("PATH", &bin)
        .output()
        .unwrap();
    let report = format!(
        "{}{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(output.status.success(), "the worker failed:\n{report}");
    // A filter that matched nothing would pass without running the case.
    assert!(done.exists(), "the worker didn't run the case:\n{report}");
    assert_eq!(
        fs::read_to_string(&calls).unwrap_or_default(),
        "",
        "git ran in the case"
    );
}

/// Git itself blocks on these FIFOs, so the helper is checked on its own.
fn fifo_case() {
    let directory = tempfile::tempdir().unwrap();
    let top = directory.path().canonicalize().unwrap();
    // No git runs in these, so nothing is left blocked on their FIFOs once the test ends.
    let fifo_head = top.join("fifo-head");
    handmade_repository(&fifo_head);
    let fifo_common = top.join("fifo-commondir");
    handmade_repository(&fifo_common);
    // Each is a repository until its FIFO goes in.
    assert!(repository_at(&fifo_head).is_some());
    assert!(repository_at(&fifo_common).is_some());
    fs::remove_file(fifo_head.join(".git/HEAD")).unwrap();
    mkfifo(&fifo_head.join(".git/HEAD"));
    mkfifo(&fifo_common.join(".git/commondir"));
    let (sender, receiver) = mpsc::channel();
    std::thread::spawn(move || {
        let _ = sender.send((repository_at(&fifo_head), repository_at(&fifo_common)));
    });
    let answers = receiver
        .recv_timeout(Duration::from_secs(5))
        .expect("repository_at waited on a FIFO");
    assert_eq!(answers, (None, None));
}
