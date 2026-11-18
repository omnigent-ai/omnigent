use super::*;

#[test]
fn local_builds_are_named_dev() {
    assert_eq!(display_version(""), "dev");
    assert_eq!(display_version("0.0.0"), "dev");
    assert_eq!(display_version("0.18.1"), "0.18.1");
}
