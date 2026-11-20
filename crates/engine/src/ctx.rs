use std::collections::HashSet;
use std::fs;
use std::io::ErrorKind;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};

use syncret_core::{Ctx, Error, Hash, Item};
use syncret_store::{Before, BlobStore, FsBlobs, Meta, clear_temps, write_atomic};

// a failure the tests inject once at one numbered step, before or after it acts
#[derive(Default)]
pub(crate) struct Faults {
    step: AtomicUsize,
    fail_at: Option<usize>,
    after: bool,
    fired: AtomicBool,
}

impl Faults {
    #[cfg(test)]
    pub(crate) fn at(step: usize, after: bool) -> Self {
        Self {
            step: AtomicUsize::new(0),
            fail_at: Some(step),
            after,
            fired: AtomicBool::new(false),
        }
    }

    #[cfg(test)]
    pub(crate) fn steps(&self) -> usize {
        self.step.load(Ordering::SeqCst)
    }

    fn tick(&self, after: bool) -> Result<(), Error> {
        if after != self.after {
            return Ok(());
        }
        let step = self.step.fetch_add(1, Ordering::SeqCst);
        if self.fail_at == Some(step) && !self.fired.swap(true, Ordering::SeqCst) {
            return Err(Error::Io(format!("injected fault at step {step}")));
        }
        Ok(())
    }
}

// the undo journal of one running apply
struct Journal<'a> {
    meta: &'a Mutex<Meta>,
    apply: i64,
    seen: Mutex<HashSet<PathBuf>>,
}

// the ctx a node hands to kinds, writes are only allowed inside a journaled apply
pub(crate) struct NodeCtx<'a> {
    item: &'a Item,
    blobs: &'a FsBlobs,
    journal: Option<Journal<'a>>,
    faults: &'a Faults,
}

impl<'a> NodeCtx<'a> {
    pub(crate) fn reading(item: &'a Item, blobs: &'a FsBlobs, faults: &'a Faults) -> Self {
        Self {
            item,
            blobs,
            journal: None,
            faults,
        }
    }

    pub(crate) fn journaled(
        item: &'a Item,
        blobs: &'a FsBlobs,
        meta: &'a Mutex<Meta>,
        apply: i64,
        faults: &'a Faults,
    ) -> Self {
        Self {
            item,
            blobs,
            journal: Some(Journal {
                meta,
                apply,
                seen: Mutex::new(HashSet::new()),
            }),
            faults,
        }
    }

    // record what path and any folders the change creates held before, then allow the change
    fn guard(&self, path: &Path) -> Result<(), Error> {
        let journal = self
            .journal
            .as_ref()
            .ok_or_else(|| Error::Io("writes are only allowed during an apply".into()))?;
        let mut missing: Vec<&Path> = path
            .ancestors()
            .skip(1)
            .take_while(|p| !p.as_os_str().is_empty() && fs::symlink_metadata(p).is_err())
            .collect();
        missing.reverse();
        missing.push(path);
        let mut seen = journal.seen.lock().expect("journal lock");
        for p in missing {
            if seen.insert(p.to_path_buf()) {
                let before = before(self.blobs, p)?;
                journal
                    .meta
                    .lock()
                    .expect("meta lock")
                    .record_undo(journal.apply, &p.to_string_lossy(), &before)
                    .map_err(store)?;
            }
        }
        self.faults.tick(false)
    }
}

impl Ctx for NodeCtx<'_> {
    fn item(&self) -> &Item {
        self.item
    }

    fn put_blob(&self, bytes: &[u8]) -> Result<Hash, Error> {
        self.faults.tick(false)?;
        let hash = self.blobs.put(bytes).map_err(store)?;
        self.faults.tick(true)?;
        Ok(hash)
    }

    fn get_blob(&self, hash: &Hash) -> Result<Vec<u8>, Error> {
        self.blobs.get(hash).map_err(store)
    }

    fn write(&self, path: &Path, bytes: &[u8], mode: Option<u32>) -> Result<(), Error> {
        self.guard(path)?;
        write_atomic(path, bytes).map_err(io)?;
        set_mode(path, mode)?;
        self.faults.tick(true)
    }

    fn remove(&self, path: &Path) -> Result<(), Error> {
        self.guard(path)?;
        remove(path)?;
        self.faults.tick(true)
    }

    fn link(&self, path: &Path, target: &Path) -> Result<(), Error> {
        self.guard(path)?;
        remove(path)?;
        make_link(path, target)?;
        self.faults.tick(true)
    }
}

// what path holds now, with file and link contents saved as blobs
fn before(blobs: &FsBlobs, path: &Path) -> Result<Before, Error> {
    let meta = match fs::symlink_metadata(path) {
        Ok(meta) => meta,
        Err(e) if e.kind() == ErrorKind::NotFound => return Ok(Before::Missing),
        Err(e) => return Err(io(e)),
    };
    if meta.file_type().is_symlink() {
        let target = fs::read_link(path).map_err(io)?;
        let hash = blobs
            .put(target.to_string_lossy().as_bytes())
            .map_err(store)?;
        return Ok(Before::Link { hash });
    }
    if meta.is_dir() {
        return Ok(Before::Folder);
    }
    let hash = blobs.put(&fs::read(path).map_err(io)?).map_err(store)?;
    Ok(Before::File {
        hash,
        mode: mode(&meta),
    })
}

// put path back to what it held, safe to repeat
pub(crate) fn restore(blobs: &FsBlobs, path: &Path, before: &Before) -> Result<(), Error> {
    clear_temps(path).map_err(io)?;
    match before {
        Before::Missing => {
            if fs::symlink_metadata(path).is_ok_and(|m| m.is_dir()) {
                // a folder the apply created goes only once it is empty again
                if let Err(e) = fs::remove_dir(path)
                    && e.kind() != ErrorKind::DirectoryNotEmpty
                {
                    return Err(io(e));
                }
                Ok(())
            } else {
                remove(path)
            }
        }
        Before::File { hash, mode } => {
            let bytes = blobs.get(hash).map_err(store)?;
            if fs::symlink_metadata(path).is_ok_and(|m| m.is_dir()) {
                fs::remove_dir(path).map_err(io)?;
            }
            write_atomic(path, &bytes).map_err(io)?;
            set_mode(path, Some(*mode))
        }
        Before::Link { hash } => {
            let target = String::from_utf8_lossy(&blobs.get(hash).map_err(store)?).into_owned();
            if fs::symlink_metadata(path).is_ok_and(|m| m.is_dir()) {
                fs::remove_dir(path).map_err(io)?;
            }
            remove(path)?;
            make_link(path, Path::new(&target))
        }
        Before::Folder => {
            if fs::symlink_metadata(path).is_ok_and(|m| !m.is_dir()) {
                remove(path)?;
            }
            fs::create_dir_all(path).map_err(io)
        }
    }
}

// remove a file, link or empty folder, missing is fine
fn remove(path: &Path) -> Result<(), Error> {
    let result = match fs::symlink_metadata(path) {
        Ok(meta) if meta.is_dir() => fs::remove_dir(path),
        Ok(_) => fs::remove_file(path),
        Err(e) => Err(e),
    };
    match result {
        Err(e) if e.kind() != ErrorKind::NotFound => Err(io(e)),
        _ => Ok(()),
    }
}

#[cfg(unix)]
fn make_link(path: &Path, target: &Path) -> Result<(), Error> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(io)?;
    }
    std::os::unix::fs::symlink(target, path).map_err(io)
}

#[cfg(not(unix))]
fn make_link(_path: &Path, _target: &Path) -> Result<(), Error> {
    Err(Error::Io(
        "links are not supported on this system yet".into(),
    ))
}

#[cfg(unix)]
fn mode(meta: &fs::Metadata) -> u32 {
    use std::os::unix::fs::PermissionsExt;
    meta.permissions().mode() & 0o7777
}

#[cfg(not(unix))]
fn mode(_meta: &fs::Metadata) -> u32 {
    0o644
}

#[cfg(unix)]
fn set_mode(path: &Path, mode: Option<u32>) -> Result<(), Error> {
    use std::os::unix::fs::PermissionsExt;
    match mode {
        Some(mode) => fs::set_permissions(path, fs::Permissions::from_mode(mode)).map_err(io),
        None => Ok(()),
    }
}

#[cfg(not(unix))]
fn set_mode(_path: &Path, _mode: Option<u32>) -> Result<(), Error> {
    Ok(())
}

pub(crate) fn io(e: std::io::Error) -> Error {
    Error::Io(e.to_string())
}

pub(crate) fn store(e: syncret_store::Error) -> Error {
    Error::Io(e.to_string())
}
