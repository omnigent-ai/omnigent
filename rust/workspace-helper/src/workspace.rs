//! Open workspaces: their root, ignore rules, and file lists.

use std::{
    collections::HashMap,
    path::PathBuf,
    sync::{Arc, Mutex},
};

use crate::{
    ignore_rules::IgnoreRules,
    index_slot::{IndexSlot, WorkspaceFiles},
    workspace_path::{PathError, WorkspaceRoot},
};

pub struct Workspace {
    pub id: String,
    pub root: WorkspaceRoot,
    pub repository_root: Option<PathBuf>,
    pub rules: Arc<IgnoreRules>,
    /// Without ignored paths, then with them.
    files: [Arc<IndexSlot>; 2],
    /// The directories a priority text search looks at before the rest.
    pub changed_directories: crate::search_priority::ChangedDirectoriesCache,
}

impl Workspace {
    fn new(id: String, root: WorkspaceRoot, global_excludes: Option<PathBuf>) -> Self {
        let rules = Arc::new(IgnoreRules::new(root.path(), global_excludes));
        let lister = Arc::new(WorkspaceFiles::new(
            root.path().to_path_buf(),
            rules.clone(),
        ));
        Self {
            id,
            repository_root: rules.repository_root(root.path()),
            files: [
                IndexSlot::new(false, lister.clone()),
                IndexSlot::new(true, lister),
            ],
            rules,
            root,
            changed_directories: Default::default(),
        }
    }

    /// The searchable file list, with or without ignored paths.
    pub fn files(&self, include_ignored: bool) -> &Arc<IndexSlot> {
        &self.files[include_ignored as usize]
    }

    /// Stops the file lists' refreshes, killing any Git they run.
    fn shutdown(&self) {
        for slot in &self.files {
            slot.shutdown();
        }
    }
}

struct OpenWorkspace {
    workspace: Arc<Workspace>,
    references: usize,
}

/// 128 random bits from the kernel, as hex.
fn random_id() -> Result<String, PathError> {
    let mut bytes = [0u8; 16];
    std::io::Read::read_exact(&mut std::fs::File::open("/dev/urandom")?, &mut bytes)?;
    Ok(bytes.iter().map(|byte| format!("{byte:02x}")).collect())
}

/// Open workspaces by ID. Opening the same root twice shares its state.
pub struct Workspaces {
    open: Mutex<HashMap<String, OpenWorkspace>>,
    global_excludes: Option<PathBuf>,
}

impl Workspaces {
    pub fn new(global_excludes: Option<PathBuf>) -> Self {
        Self {
            open: Mutex::default(),
            global_excludes,
        }
    }

    pub fn open(&self, root: &str) -> Result<Arc<Workspace>, PathError> {
        let root = WorkspaceRoot::open(root)?;
        let mut open = self.open.lock().unwrap();
        if let Some(existing) = open
            .values_mut()
            .find(|entry| entry.workspace.root.path() == root.path())
        {
            existing.references += 1;
            return Ok(existing.workspace.clone());
        }
        // Unguessable and never reused, so an id a client kept from an earlier
        // helper process can't name another root in this one.
        let id = loop {
            let id = random_id()?;
            if !open.contains_key(&id) {
                break id;
            }
        };
        let workspace = Arc::new(Workspace::new(
            id.clone(),
            root,
            self.global_excludes.clone(),
        ));
        open.insert(
            id,
            OpenWorkspace {
                workspace: workspace.clone(),
                references: 1,
            },
        );
        Ok(workspace)
    }

    pub fn get(&self, id: &str) -> Option<Arc<Workspace>> {
        self.open
            .lock()
            .unwrap()
            .get(id)
            .map(|entry| entry.workspace.clone())
    }

    /// Releases one open; the last close drops the workspace's state.
    pub fn close(&self, id: &str) -> bool {
        let mut open = self.open.lock().unwrap();
        let Some(entry) = open.get_mut(id) else {
            return false;
        };
        entry.references -= 1;
        if entry.references == 0
            && let Some(closed) = open.remove(id)
        {
            closed.workspace.shutdown();
        }
        true
    }
}
