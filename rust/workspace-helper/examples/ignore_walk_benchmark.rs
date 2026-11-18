//! Times the walks that ignore matching shapes: the file index build and a
//! text search that matches nothing, over the whole tree and below its root.
//! `cargo run --release --example ignore_walk_benchmark -- <tree> [runs] [index|search]`
//! generates a rule-heavy tree at `<tree>` when it doesn't exist.

use std::{
    fs,
    path::{Path, PathBuf},
    process::Command,
    time::Instant,
};

use workspace_helper::{
    file_index::FileIndex,
    ignore_rules::IgnoreRules,
    text_search::{TextQuery, search_all},
    walk::IgnoreMode,
};

/// A repository with ignore files of every kind at many levels, nested
/// repositories, and ignored directories.
fn generate(root: &Path) {
    let write = |path: PathBuf, contents: &str| {
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, contents).unwrap();
    };
    fs::create_dir_all(root).unwrap();
    Command::new("git")
        .args(["init", "-q"])
        .current_dir(root)
        .status()
        .unwrap();
    write(root.join(".gitignore"), "*.log\ntarget/\n!keep.log\n");
    write(root.join(".ignore"), "*.snap\n");
    for package in 0..40 {
        let package_root = root.join(format!("package{package}"));
        write(package_root.join(".gitignore"), "generated/\n*.tmp\n");
        if package % 3 == 0 {
            write(package_root.join(".rgignore"), "fixtures/\n");
        }
        if package % 10 == 0 {
            Command::new("git")
                .args(["init", "-q"])
                .current_dir(&package_root)
                .status()
                .unwrap();
        }
        for module in 0..25 {
            let module_root = package_root.join(format!("src/module{module}"));
            if module % 5 == 0 {
                write(module_root.join(".gitignore"), "!keep.tmp\nscratch*\n");
            }
            for file in 0..20 {
                write(module_root.join(format!("file{file}.rs")), "fn main() {}\n");
            }
            write(module_root.join("debug.log"), "");
            write(module_root.join("keep.tmp"), "");
            write(module_root.join("generated/out.rs"), "");
        }
        write(package_root.join("target/debug/build.rs"), "");
    }
}

fn median_milliseconds(runs: usize, mut work: impl FnMut() -> usize) -> (f64, usize) {
    let mut times = Vec::new();
    let mut count = 0;
    for _ in 0..runs {
        let start = Instant::now();
        count = work();
        times.push(start.elapsed().as_secs_f64() * 1000.0);
    }
    times.sort_by(f64::total_cmp);
    (times[times.len() / 2], count)
}

fn main() {
    let mut arguments = std::env::args().skip(1);
    let root = PathBuf::from(arguments.next().expect("a tree path"));
    let runs: usize = arguments.next().map_or(7, |runs| runs.parse().unwrap());
    let only = arguments.next();
    let (index, search) = (
        only.as_deref() != Some("search"),
        only.as_deref() != Some("index"),
    );
    if !root.exists() {
        generate(&root);
    }
    let root = fs::canonicalize(root).unwrap();
    let rules = IgnoreRules::new(&root, None);
    for mode in [IgnoreMode::Git, IgnoreMode::Ripgrep]
        .into_iter()
        .filter(|_| index)
    {
        let (time, paths) =
            median_milliseconds(runs, || FileIndex::build(&root, mode, &rules).len());
        println!("index build {mode:?}: {paths} paths, median {time:.1} ms");
    }
    let below_root: Vec<String> = fs::read_dir(&root)
        .unwrap()
        .filter_map(|entry| entry.ok())
        .filter(|entry| entry.file_type().is_ok_and(|kind| kind.is_dir()))
        .map(|entry| entry.file_name().to_string_lossy().into_owned())
        .filter(|name| name != ".git")
        .take(8)
        .collect();
    for (label, directories) in [("whole tree", Vec::new()), ("below root", below_root)]
        .into_iter()
        .filter(|_| search)
    {
        let query = TextQuery {
            query: "no-such-text-anywhere-0d1f".to_string(),
            limit: 500,
            include_directories: directories,
            ..TextQuery::default()
        };
        let (time, files) =
            median_milliseconds(runs, || search_all(&root, &query, None).unwrap().0.len());
        println!("no-match search, {label}: {files} matches, median {time:.1} ms");
    }
}
