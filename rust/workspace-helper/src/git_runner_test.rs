use super::*;

fn alive(pid: i32) -> bool {
    // SAFETY: signal 0 only checks that the process exists.
    unsafe { libc::kill(pid, 0) == 0 }
}

/// Long enough for a shell to start even when process launches are slow.
const TEST_DEADLINE: Duration = Duration::from_secs(8);

async fn call(stdout_bytes: usize, deadline: Duration) -> Result<GitOutput, GitError> {
    run_git(
        Path::new("/"),
        None,
        Admission::Helper,
        &[],
        &GitCommand::Version,
        None,
        CallLimits {
            deadline: Instant::now() + deadline,
            stdout_bytes,
        },
    )
    .await
}

#[tokio::test(flavor = "current_thread")]
async fn the_deadline_kills_and_reaps_the_whole_process_group() {
    let directory = tempfile::tempdir().unwrap();
    let pid_file = directory.path().join("grandchild");
    fake_git(&format!(
        "sleep 30 & echo $! > '{}.tmp' && mv '{0}.tmp' '{0}'\nwait",
        pid_file.display()
    ));
    let started = std::time::Instant::now();
    assert_eq!(
        call(1024, TEST_DEADLINE).await.unwrap_err(),
        GitError::Timeout
    );
    assert!(started.elapsed() < TEST_DEADLINE + Duration::from_secs(4));
    // The orphaned sleep is reaped by init shortly after the kill.
    assert!(
        eventually_gone(started_process(&pid_file)),
        "the grandchild survived"
    );
}

#[tokio::test(flavor = "current_thread")]
async fn output_beyond_the_cap_stops_the_command() {
    fake_git("yes git");
    let started = std::time::Instant::now();
    assert_eq!(
        call(100_000, Duration::from_secs(20)).await.unwrap_err(),
        GitError::OutputTooLarge(100_000)
    );
    assert!(started.elapsed() < Duration::from_secs(5));
}

#[tokio::test(flavor = "current_thread")]
async fn stderr_is_kept_bounded_and_exit_codes_are_reported() {
    fake_git("head -c 1000000 /dev/zero >&2; echo out; exit 3");
    let output = call(1024, Duration::from_secs(20)).await.unwrap();
    assert!(!output.success);
    assert_eq!(output.exit_code, Some(3));
    assert_eq!(output.stdout, b"out\n");
    assert!(output.stderr.len() <= STDERR_BYTES);
}

#[tokio::test(flavor = "current_thread")]
async fn a_command_stopped_by_an_outside_signal_is_reported_and_not_rerun() {
    let directory = tempfile::tempdir().unwrap();
    let attempts = directory.path().join("attempts");
    fake_git(&format!(
        "echo x >> '{}'\nkill -TERM $$",
        attempts.display()
    ));
    let output = call(1024, Duration::from_secs(20)).await.unwrap();
    assert!(!output.success);
    assert_eq!(output.signal, Some(libc::SIGTERM));
    assert_eq!(
        failure_of(&output, "version"),
        GitError::Interrupted {
            command: "version".into(),
            signal: libc::SIGTERM,
            ran_for_ms: output.ran_for.as_millis(),
        }
    );
    assert!(
        failure_of(&output, "version")
            .to_string()
            .starts_with("git version was stopped by signal 15 after "),
    );
    assert_eq!(
        std::fs::read_to_string(&attempts).unwrap().lines().count(),
        1
    );
}

#[tokio::test(flavor = "current_thread")]
async fn a_git_killed_from_outside_is_reported_with_how_long_it_ran() {
    fake_git("sleep 0.2\nkill -KILL $$");
    let output = call(1024, Duration::from_secs(20)).await.unwrap();
    assert_eq!(output.signal, Some(libc::SIGKILL));
    assert!(
        output.ran_for >= Duration::from_millis(200),
        "{:?}",
        output.ran_for
    );
    assert!(output.ran_for < Duration::from_secs(20));
}

thread_local! {
    /// An exit observer for the calls this test thread makes, in place of waitid.
    pub(super) static EXIT_OBSERVER: std::cell::Cell<Option<ExitObserver>> =
        const { std::cell::Cell::new(None) };
}

/// A failed watch keeps a call open until its deadline, so these calls keep it short.
const FAILED_WATCH_DEADLINE: Duration = Duration::from_millis(1500);

/// An exit observer that fails at once, as waitid can with an error.
fn failing_observer(_process_id: libc::pid_t) -> bool {
    false
}

/// Makes this thread's calls use the failing observer, outside the shared
/// admission slots, until dropped.
struct FailingExitWatch;

impl FailingExitWatch {
    fn install() -> Self {
        EXIT_OBSERVER.set(Some(failing_observer));
        Self
    }
}

impl Drop for FailingExitWatch {
    fn drop(&mut self) {
        EXIT_OBSERVER.set(None);
    }
}

#[tokio::test(flavor = "current_thread")]
async fn a_failed_exit_watch_leaves_git_unreaped_until_the_group_is_killed() {
    use std::os::unix::process::CommandExt;
    let child = std::process::Command::new("/bin/sh")
        .args(["-c", "exit 3"])
        .process_group(0)
        .spawn()
        .unwrap();
    let pid = child.id() as i32;
    let (guard, exited) = ChildGuard::watch_with(child, failing_observer);
    assert!(exited.await.is_err(), "an unseen exit is never reported");
    tokio::time::sleep(Duration::from_millis(300)).await;
    // The leader has exited but stays a zombie, so its group ID can't be reused.
    assert!(
        alive(pid),
        "the leader was reaped before the group was killed"
    );
    let status = guard
        .finish()
        .await
        .expect("the waiter reaps after the kill");
    assert_eq!(status.code(), Some(3));
    assert!(eventually_gone(pid));
}

#[tokio::test(flavor = "current_thread")]
async fn a_failed_exit_watch_ends_the_call_at_its_deadline_without_killing_git_early() {
    let _watch = FailingExitWatch::install();
    let directory = tempfile::tempdir().unwrap();
    let ran = directory.path().join("ran");
    fake_git(&format!(
        "sleep 0.3; echo finished > '{}'; exit 0",
        ran.display()
    ));
    let started = std::time::Instant::now();
    assert_eq!(
        call(1024, FAILED_WATCH_DEADLINE).await.unwrap_err(),
        GitError::Timeout
    );
    assert!(started.elapsed() >= FAILED_WATCH_DEADLINE);
    // Git ran to its end: nothing killed it before its exit.
    assert_eq!(std::fs::read_to_string(&ran).unwrap(), "finished\n");
}

#[tokio::test(flavor = "current_thread")]
async fn a_failed_exit_watch_still_kills_descendants_at_the_deadline() {
    let _watch = FailingExitWatch::install();
    let directory = tempfile::tempdir().unwrap();
    for (name, script) in [
        (
            "holding output",
            "sleep 30 & echo $! > '{0}.tmp' && mv '{0}.tmp' '{0}'\nexit 0",
        ),
        (
            "detached",
            "(exec >/dev/null 2>&1; exec sleep 30) &\necho $! > '{0}.tmp' && mv '{0}.tmp' '{0}'\necho done",
        ),
    ] {
        let pid_file = directory.path().join(name.replace(' ', "-"));
        fake_git(&script.replace("{0}", &pid_file.display().to_string()));
        assert_eq!(
            call(1024, FAILED_WATCH_DEADLINE).await.unwrap_err(),
            GitError::Timeout,
            "{name}"
        );
        assert!(
            eventually_gone(started_process(&pid_file)),
            "{name}: the descendant survived"
        );
    }
}

#[tokio::test(flavor = "current_thread")]
async fn a_failed_exit_watch_still_kills_the_group_when_the_call_is_cancelled() {
    let _watch = FailingExitWatch::install();
    let directory = tempfile::tempdir().unwrap();
    let pid_file = silent_lingering_git(directory.path());
    let started = async {
        while !pid_file.exists() {
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    };
    tokio::select! {
        result = call(1024, Duration::from_secs(60)) => panic!("the call finished: {result:?}"),
        () = started => {}
    }
    assert!(
        eventually_gone(started_process(&pid_file)),
        "the child survived cancellation"
    );
}

/// Writes the fake Git's process ID, closes its output, and keeps running.
fn silent_lingering_git(directory: &Path) -> std::path::PathBuf {
    let pid_file = directory.join("pid");
    fake_git(&format!(
        "echo $$ > '{0}.tmp' && mv '{0}.tmp' '{0}'\nexec >&- 2>&-\nexec sleep 30",
        pid_file.display()
    ));
    pid_file
}

/// The process ID the fake Git wrote once it started.
fn started_process(pid_file: &Path) -> i32 {
    std::fs::read_to_string(pid_file)
        .expect("the fake Git never started")
        .trim()
        .parse()
        .unwrap()
}

fn eventually_gone(pid: i32) -> bool {
    (0..100).any(|_| {
        std::thread::sleep(Duration::from_millis(20));
        !alive(pid)
    })
}

#[tokio::test(flavor = "current_thread")]
async fn a_child_that_closes_its_output_but_keeps_running_is_killed_at_the_deadline() {
    let directory = tempfile::tempdir().unwrap();
    let pid_file = silent_lingering_git(directory.path());
    let started = std::time::Instant::now();
    assert_eq!(
        call(1024, TEST_DEADLINE).await.unwrap_err(),
        GitError::Timeout
    );
    assert!(started.elapsed() < TEST_DEADLINE + Duration::from_secs(4));
    assert!(
        eventually_gone(started_process(&pid_file)),
        "the child survived its deadline"
    );
}

#[tokio::test(flavor = "current_thread")]
async fn cancelling_a_call_kills_a_child_that_closed_its_output() {
    let directory = tempfile::tempdir().unwrap();
    let pid_file = silent_lingering_git(directory.path());
    let started = async {
        while !pid_file.exists() {
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    };
    // The call is dropped as soon as the child is running.
    tokio::select! {
        result = call(1024, Duration::from_secs(60)) => panic!("the call finished: {result:?}"),
        () = started => {}
    }
    assert!(
        eventually_gone(started_process(&pid_file)),
        "the child survived cancellation"
    );
}

#[tokio::test(flavor = "current_thread")]
async fn the_environment_carries_no_inherited_git_settings() {
    fake_git("env; echo \"ARGS $*\"");
    // Cargo sets this for the test process; the child must not inherit it.
    assert!(std::env::var_os("CARGO_MANIFEST_DIR").is_some());
    let output = call(64 * 1024, Duration::from_secs(20)).await.unwrap();
    let text = String::from_utf8(output.stdout).unwrap();
    assert!(!text.contains("CARGO_MANIFEST_DIR"));
    for (name, value) in FIXED_VARIABLES {
        assert!(text.contains(&format!("{name}={value}\n")), "{name}");
    }
    assert!(text.contains("-c core.fsmonitor=false"));
    assert!(text.contains("-c protocol.allow=never"));
}

#[tokio::test(flavor = "current_thread")]
async fn descendants_that_outlive_git_and_hold_its_output_are_killed_at_the_deadline() {
    let directory = tempfile::tempdir().unwrap();
    let pid_file = directory.path().join("descendant");
    fake_git(&format!(
        "sleep 30 & echo $! > '{0}.tmp' && mv '{0}.tmp' '{0}'\nexit 0",
        pid_file.display()
    ));
    assert_eq!(
        call(1024, TEST_DEADLINE).await.unwrap_err(),
        GitError::Timeout
    );
    assert!(
        eventually_gone(started_process(&pid_file)),
        "the descendant survived"
    );
}

#[tokio::test(flavor = "current_thread")]
async fn descendants_left_behind_by_a_finished_call_are_killed() {
    let directory = tempfile::tempdir().unwrap();
    let pid_file = directory.path().join("descendant");
    fake_git(&format!(
        "(exec >/dev/null 2>&1; exec sleep 30) &\necho $! > '{0}.tmp' && mv '{0}.tmp' '{0}'\necho done",
        pid_file.display()
    ));
    let output = call(1024, TEST_DEADLINE).await.unwrap();
    assert_eq!(output.stdout, b"done\n");
    assert!(
        eventually_gone(started_process(&pid_file)),
        "the descendant survived"
    );
}

/// Runs every future concurrently on this task, returning results in order.
async fn join_all<F: std::future::Future>(futures: impl IntoIterator<Item = F>) -> Vec<F::Output> {
    let mut futures: Vec<_> = futures
        .into_iter()
        .map(|future| Some(Box::pin(future)))
        .collect();
    let mut results: Vec<Option<F::Output>> = futures.iter().map(|_| None).collect();
    std::future::poll_fn(|context| {
        let mut pending = false;
        for (slot, result) in futures.iter_mut().zip(results.iter_mut()) {
            if let Some(future) = slot {
                match future.as_mut().poll(context) {
                    std::task::Poll::Ready(output) => {
                        *result = Some(output);
                        *slot = None;
                    }
                    std::task::Poll::Pending => pending = true,
                }
            }
        }
        if pending {
            std::task::Poll::Pending
        } else {
            std::task::Poll::Ready(())
        }
    })
    .await;
    results.into_iter().map(Option::unwrap).collect()
}

/// A fake Git that answers discovery for `root` and records, for every
/// other command, how many such commands were running when it started.
fn counting_git(root: &Path) {
    let running = root.join("running");
    std::fs::create_dir(&running).unwrap();
    fake_git(&format!(
        r#"{discovery}touch '{running}'/$$
ls '{running}' | wc -l >> '{root}/counts'
sleep 0.3
rm '{running}'/$$"#,
        discovery = fake_discovery(root),
        root = root.display(),
        running = running.display(),
    ));
}

fn most_running(root: &Path) -> usize {
    std::fs::read_to_string(root.join("counts"))
        .unwrap()
        .lines()
        .map(|count| count.trim().parse::<usize>().unwrap())
        .max()
        .unwrap()
}

fn limits() -> CallLimits {
    CallLimits {
        deadline: Instant::now() + Duration::from_secs(30),
        stdout_bytes: 1024,
    }
}

#[tokio::test(flavor = "current_thread")]
async fn requests_that_open_the_same_repository_share_its_limit() {
    let directory = tempfile::tempdir().unwrap();
    let root = std::fs::canonicalize(directory.path()).unwrap();
    std::fs::create_dir(root.join(".git")).unwrap();
    counting_git(&root);
    // Two requests discover the repository on their own, at the same time.
    let (first, second) = tokio::join!(
        GitRepository::discover(&root, limits().deadline),
        GitRepository::discover(&root, limits().deadline),
    );
    let (first, second) = (first.unwrap().unwrap(), second.unwrap().unwrap());
    assert!(Arc::ptr_eq(&first.slots, &second.slots));
    let calls = (0..6).map(|index| {
        let repository = if index % 2 == 0 { &first } else { &second };
        repository.run_unchecked(&GitCommand::IsShallow, None, limits())
    });
    for output in join_all(calls).await {
        assert!(output.unwrap().success);
    }
    assert!(most_running(&root) <= REPOSITORY_CONCURRENCY);
}

#[tokio::test(flavor = "current_thread")]
async fn background_scans_leave_every_interactive_slot_free() {
    let directory = tempfile::tempdir().unwrap();
    let root = std::fs::canonicalize(directory.path()).unwrap();
    // Untracked discovery hangs; everything else answers at once.
    fake_git(&format!(
        r#"{discovery}case "$*" in *--others*) sleep 60;; esac
printf 'ok\0'"#,
        discovery = fake_discovery(&root),
    ));
    let repository = Arc::new(
        GitRepository::discover(&root, limits().deadline)
            .await
            .unwrap()
            .unwrap(),
    );
    // More scans than there are background slots, all held.
    let scans: Vec<_> = (0..BACKGROUND_CONCURRENCY + 1)
        .map(|_| {
            let repository = repository.clone();
            tokio::spawn(async move {
                repository
                    .run_in_background(&GitCommand::ListUntracked, limits())
                    .await
            })
        })
        .collect();
    tokio::time::sleep(Duration::from_millis(300)).await;

    let interactive = CallLimits {
        deadline: Instant::now() + Duration::from_secs(5),
        stdout_bytes: 1024,
    };
    // A tracked listing, a working-copy read, and a new discovery in the same repository.
    for _ in 0..REPOSITORY_CONCURRENCY + 1 {
        repository
            .run(&GitCommand::ListTracked, None, interactive)
            .await
            .unwrap();
        repository
            .run_unchecked(&GitCommand::IsShallow, None, interactive)
            .await
            .unwrap();
    }
    assert!(
        GitRepository::discover(&root, interactive.deadline)
            .await
            .unwrap()
            .is_some()
    );
    assert!(
        scans.iter().all(|scan| !scan.is_finished()),
        "the scans are still held"
    );
    for scan in scans {
        scan.abort();
    }
}

#[tokio::test(flavor = "current_thread")]
async fn discovery_and_version_checks_wait_for_a_helper_slot() {
    let directory = tempfile::tempdir().unwrap();
    let root = std::fs::canonicalize(directory.path()).unwrap();
    counting_git(&root);
    let calls = (0..HELPER_CONCURRENCY * 2).map(|_| {
        run_git(
            &root,
            None,
            Admission::Helper,
            &[],
            &GitCommand::Version,
            None,
            limits(),
        )
    });
    for output in join_all(calls).await {
        assert!(output.unwrap().success);
    }
    assert!(most_running(&root) <= HELPER_CONCURRENCY);
}

#[test]
fn filter_driver_names_keep_their_case() {
    let records = b"filter.MixedCase.clean\nmarker-clean\0filter.MixedCase.required\ntrue\0filter.lower.smudge\nmarker\0filter.Dotted.Name.process\nmarker\0remote.Fork.promisor\ntrue\0";
    let configuration = BehaviorConfiguration::parse(records).unwrap();
    assert_eq!(
        configuration.filter_drivers.iter().collect::<Vec<_>>(),
        ["Dotted.Name", "MixedCase", "lower"]
    );
    assert!(configuration.partial_clone);
    let overrides = filter_overrides(&configuration.filter_drivers);
    assert!(overrides.contains(&"filter.MixedCase.clean=".to_string()));
    assert!(overrides.contains(&"filter.MixedCase.process=".to_string()));
    assert!(overrides.contains(&"filter.MixedCase.required=false".to_string()));
    assert!(
        !overrides
            .iter()
            .any(|setting| setting.contains("mixedcase"))
    );
    assert!(!BehaviorConfiguration::parse(b"").unwrap().partial_clone);
}

#[test]
fn filter_drivers_with_non_utf8_values_are_still_overridden() {
    let records = b"filter.Bytes.clean\n/opt/tools/cl\xe9an %f\0remote.origin.promisor\nTRUE\0";
    let configuration = BehaviorConfiguration::parse(records).unwrap();
    assert_eq!(
        configuration.filter_drivers.iter().collect::<Vec<_>>(),
        ["Bytes"]
    );
    assert!(configuration.partial_clone);
    assert!(
        filter_overrides(&configuration.filter_drivers)
            .contains(&"filter.Bytes.clean=".to_string())
    );
}

#[test]
fn keys_that_cannot_be_overridden_safely_refuse_the_repository() {
    for records in [
        &b"filter.caf\xe9.clean\nmarker\0"[..],
        &b"filter.a=b.clean\nmarker\0"[..],
    ] {
        assert!(matches!(
            BehaviorConfiguration::parse(records),
            Err(GitError::Failed(_))
        ));
    }
}

#[tokio::test(flavor = "current_thread")]
async fn an_unreadable_filter_key_stops_discovery_before_any_other_command() {
    let directory = tempfile::tempdir().unwrap();
    let root = std::fs::canonicalize(directory.path()).unwrap();
    let ran = root.join("ran");
    fake_git(&format!(
        r#"case "$*" in
*rev-parse\ --absolute-git-dir*) printf '%s\n%s\n%s\n\n' '{root}/.git' '{root}/.git' '{root}'; exit 0;;
*config\ --null*) printf 'filter.caf\351.clean\nmarker\0'; exit 0;;
esac
touch '{ran}'"#,
        root = root.display(),
        ran = ran.display(),
    ));
    let discovered = GitRepository::discover(&root, limits().deadline).await;
    assert!(matches!(discovered, Err(GitError::Failed(_))));
    assert!(
        !ran.exists(),
        "a command ran after the configuration was refused"
    );
}

#[test]
fn parses_git_versions() {
    assert_eq!(
        parse_version("git version 2.54.0 (Apple Git-157)\n"),
        Some((2, 54))
    );
    assert_eq!(parse_version("git version 2.39.5"), Some((2, 39)));
    assert_eq!(parse_version("not git"), None);
}

#[test]
fn accepts_only_full_object_names() {
    assert!(is_object_name(&"a".repeat(40)));
    assert!(is_object_name(&"0".repeat(64)));
    assert!(!is_object_name("abc123"));
    assert!(!is_object_name(&format!("{}g", "a".repeat(39))));
    assert!(!is_object_name("HEAD"));
}

#[test]
fn revisions_are_passed_after_end_of_options() {
    let arguments = GitCommand::MergeBase {
        left: "--output=x".into(),
        right: "HEAD".into(),
    }
    .arguments();
    let end = arguments
        .iter()
        .position(|argument| argument == "--end-of-options");
    let left = arguments
        .iter()
        .position(|argument| argument == "--output=x");
    assert!(end.unwrap() < left.unwrap());
}

/// The helper and background slots a call from the current thread would wait for.
fn slots_here() -> (Arc<Semaphore>, Arc<Semaphore>) {
    (super::helper_slots(), super::background_slots())
}

#[test]
fn a_thread_with_a_fake_git_waits_only_on_its_own_slots() {
    let global = std::thread::spawn(slots_here).join().unwrap();
    assert!(Arc::ptr_eq(&global.0, &HELPER_SLOTS));
    assert!(Arc::ptr_eq(&global.1, &BACKGROUND_SLOTS));
    let faked = || {
        fake_git("printf 'ok'");
        let first = slots_here();
        // Installing another script keeps the same slots.
        fake_git("printf 'again'");
        let again = slots_here();
        assert!(Arc::ptr_eq(&first.0, &again.0) && Arc::ptr_eq(&first.1, &again.1));
        assert_eq!(first.0.available_permits(), HELPER_CONCURRENCY);
        assert_eq!(first.1.available_permits(), BACKGROUND_CONCURRENCY);
        first
    };
    let one = std::thread::spawn(faked).join().unwrap();
    let other = std::thread::spawn(faked).join().unwrap();
    for (helper, background) in [&one, &other] {
        assert!(!Arc::ptr_eq(helper, &HELPER_SLOTS));
        assert!(!Arc::ptr_eq(background, &BACKGROUND_SLOTS));
    }
    assert!(!Arc::ptr_eq(&one.0, &other.0));
    assert!(!Arc::ptr_eq(&one.1, &other.1));
}
