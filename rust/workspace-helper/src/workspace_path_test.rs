use super::*;

#[test]
fn normalizes_relative_paths() {
    assert_eq!(normalize_relative("").unwrap(), "");
    assert_eq!(normalize_relative(".").unwrap(), "");
    assert_eq!(normalize_relative("./a//b/").unwrap(), "a/b");
}

#[test]
fn rejects_traversal_absolute_and_nul() {
    assert!(matches!(
        normalize_relative("../x"),
        Err(PathError::Invalid(_))
    ));
    assert!(matches!(
        normalize_relative("a/../../x"),
        Err(PathError::Invalid(_))
    ));
    assert!(matches!(
        normalize_relative("/etc"),
        Err(PathError::Invalid(_))
    ));
    assert!(matches!(
        normalize_relative("a\0b"),
        Err(PathError::Invalid(_))
    ));
}

#[test]
fn symlinks_may_not_escape_the_root() {
    let outside = tempfile::tempdir().unwrap();
    std::fs::write(outside.path().join("secret"), "x").unwrap();
    let workspace = tempfile::tempdir().unwrap();
    std::fs::create_dir(workspace.path().join("inner")).unwrap();
    std::fs::write(workspace.path().join("inner/file"), "y").unwrap();
    std::os::unix::fs::symlink(outside.path(), workspace.path().join("escape")).unwrap();
    std::os::unix::fs::symlink("inner", workspace.path().join("alias")).unwrap();
    let root = WorkspaceRoot::open(workspace.path().to_str().unwrap()).unwrap();
    assert!(matches!(
        root.resolve("escape/secret"),
        Err(PathError::OutsideWorkspace)
    ));
    let (relative, resolved) = root.resolve("alias/file").unwrap();
    assert_eq!(relative, "alias/file");
    assert!(resolved.ends_with("inner/file"));
    assert!(matches!(
        root.resolve("missing"),
        Err(PathError::NotFound(_))
    ));
}

#[test]
fn open_rejects_relative_and_missing_roots() {
    assert!(matches!(
        WorkspaceRoot::open("relative"),
        Err(PathError::Invalid(_))
    ));
    assert!(matches!(
        WorkspaceRoot::open("/definitely/not/here"),
        Err(PathError::NotFound(_))
    ));
}
