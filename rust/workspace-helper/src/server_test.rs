use super::*;

#[tokio::test]
async fn a_closed_stream_cancels_a_search_that_has_sent_nothing_and_stops_its_git() {
    let (sender, receiver) = mpsc::channel::<u8>(1);
    let cancelled = Arc::new(AtomicBool::new(false));
    let git = tokio::spawn(std::future::pending::<()>());
    let finished = cancel_when_closed(&sender, cancelled.clone(), Some(git.abort_handle()));
    drop(receiver);
    let stopped = tokio::time::timeout(std::time::Duration::from_secs(1), git).await;
    assert!(stopped.unwrap().unwrap_err().is_cancelled());
    assert!(cancelled.load(Ordering::Relaxed));
    drop(finished);
}

#[tokio::test]
async fn a_finished_search_stops_watching_so_its_stream_can_end() {
    let (sender, mut receiver) = mpsc::channel::<u8>(1);
    let cancelled = Arc::new(AtomicBool::new(false));
    let finished = cancel_when_closed(&sender, cancelled.clone(), None);
    drop(sender);
    drop(finished);
    let ended = tokio::time::timeout(std::time::Duration::from_secs(1), receiver.recv()).await;
    assert_eq!(ended.unwrap(), None, "the watcher's sender was dropped");
    assert!(!cancelled.load(Ordering::Relaxed));
}

#[test]
fn a_wait_for_git_ends_at_its_budget_on_cancel_or_when_git_gives_up() {
    let (_ready, answer) = std::sync::mpsc::channel::<Option<Vec<String>>>();
    let started = std::time::Instant::now();
    let none = wait_for_changes(
        &answer,
        std::time::Duration::from_millis(60),
        &AtomicBool::new(false),
    );
    assert_eq!(none, None);
    assert!(started.elapsed() >= std::time::Duration::from_millis(60));

    let started = std::time::Instant::now();
    let cancelled = wait_for_changes(
        &answer,
        std::time::Duration::from_secs(5),
        &AtomicBool::new(true),
    );
    assert_eq!(cancelled, None);
    assert!(started.elapsed() < std::time::Duration::from_millis(500));

    let (ready, answer) = std::sync::mpsc::channel();
    ready.send(Some(vec!["src".to_string()])).unwrap();
    assert_eq!(
        wait_for_changes(
            &answer,
            std::time::Duration::from_secs(1),
            &AtomicBool::new(false)
        ),
        Some(vec!["src".to_string()])
    );
    drop(ready);
    assert_eq!(
        wait_for_changes(
            &answer,
            std::time::Duration::from_secs(1),
            &AtomicBool::new(false)
        ),
        None,
        "git gave up"
    );
}
