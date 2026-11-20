use std::fs::{self, File, TryLockError};
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use syncret_core::{Error, Hash, Hlc, Item, Kind, NodeId, NodeKey, SystemClock, Version};
use syncret_kinds::FileKind;
use syncret_store::{FsBlobs, Meta, write_atomic};

use crate::ctx::{Faults, NodeCtx, io, store};
use crate::journal::{self, lock};

// one device holding its key, its store and the kinds it syncs
pub struct Node {
    key: NodeKey,
    pub(crate) blobs: FsBlobs,
    pub(crate) meta: Mutex<Meta>,
    hlc: Mutex<Hlc<SystemClock>>,
    // one capture or apply at a time, so neither sees the other half done
    ops: tokio::sync::Mutex<()>,
    pub(crate) faults: Faults,
    _lock: File,
}

impl Node {
    // open a data folder, refusing one another process holds, and finish any interrupted apply
    pub fn open(data: &Path) -> Result<Self, Error> {
        fs::create_dir_all(data).map_err(io)?;
        let lock = File::create(data.join("lock")).map_err(io)?;
        match lock.try_lock() {
            Ok(()) => {}
            Err(TryLockError::WouldBlock) => {
                return Err(Error::Io(format!(
                    "{} is in use by another process",
                    data.display()
                )));
            }
            Err(TryLockError::Error(e)) => return Err(io(e)),
        }
        let key = load_key(&data.join("node.key"))?;
        let blobs = FsBlobs::open(data).map_err(store)?;
        let meta = Meta::open(data).map_err(store)?;
        let newest = meta.newest_clock().map_err(store)?;
        let node = Self {
            hlc: Mutex::new(Hlc::new(key.id(), SystemClock, newest)),
            key,
            blobs,
            meta: Mutex::new(meta),
            ops: tokio::sync::Mutex::new(()),
            faults: Faults::default(),
            _lock: lock,
        };
        journal::recover(&node.blobs, &node.meta)?;
        Ok(node)
    }

    pub fn id(&self) -> NodeId {
        self.key.id()
    }

    pub fn heads(&self, item: &str) -> Result<Vec<Version>, Error> {
        lock(&self.meta).heads(item).map_err(store)
    }

    // snapshot an item and commit a version when its content is new, none otherwise
    pub async fn capture(&self, item: &Item) -> Result<Option<Version>, Error> {
        let _op = self.ops.lock().await;
        let kind = kind(&item.kind)?;
        let snapshot = kind
            .capture(&NodeCtx::reading(item, &self.blobs, &self.faults))
            .await?;
        let mut meta = lock(&self.meta);
        let local = meta.local(&item.id).map_err(store)?;
        // the disk still holds what this node last committed or applied, so nothing changed
        if local
            .as_ref()
            .is_some_and(|v| v.content == snapshot.content)
        {
            return Ok(None);
        }
        // content already in the history, as on a device that has not applied yet
        if local.is_none()
            && let Some(head) = meta
                .heads(&item.id)
                .map_err(store)?
                .into_iter()
                .find(|h| h.content == snapshot.content)
        {
            meta.set_local(&head).map_err(store)?;
            return Ok(None);
        }
        // a local edit descends from what the disk held, a newer head makes a fork to merge
        let parents = local.iter().map(Version::id).collect();
        let hlc = self.hlc.lock().expect("clock lock").now();
        let version = Version::new(
            &self.key,
            &item.id,
            parents,
            snapshot.content,
            hlc,
            snapshot.meta,
        );
        meta.insert(&version).map_err(store)?;
        meta.set_local(&version).map_err(store)?;
        Ok(Some(version))
    }

    // apply the single head of an item, a conflict waits for phase 3 merging
    pub async fn apply(&self, item: &Item) -> Result<Hash, Error> {
        let heads = self.heads(&item.id)?;
        match heads.as_slice() {
            [head] => {
                self.apply_version(item, &head.id()).await?;
                Ok(head.id())
            }
            [] => Err(Error::Io(format!("{} has no versions", item.id))),
            _ => Err(Error::Io(format!(
                "{} has {} heads to merge",
                item.id,
                heads.len()
            ))),
        }
    }

    // apply one stored version of an item through the journal
    pub async fn apply_version(&self, item: &Item, id: &Hash) -> Result<(), Error> {
        let _op = self.ops.lock().await;
        let version = lock(&self.meta)
            .get(id)
            .map_err(store)?
            .ok_or_else(|| Error::Io(format!("no version {id}")))?;
        if version.item != item.id {
            return Err(Error::Io(format!("version {id} is not of {}", item.id)));
        }
        let merged = syncret_core::Snapshot {
            content: version.content,
            meta: version.meta.clone(),
        };
        journal::apply(
            kind(&item.kind)?,
            item,
            &self.blobs,
            &self.meta,
            &merged,
            &self.faults,
        )
        .await?;
        lock(&self.meta).set_local(&version).map_err(store)
    }

    #[cfg(test)]
    pub(crate) fn with_faults(mut self, faults: Faults) -> Self {
        self.faults = faults;
        self
    }
}

fn kind(id: &str) -> Result<&'static dyn Kind, Error> {
    match id {
        "file" => Ok(&FileKind),
        other => Err(Error::Recipe(format!("kind {other} is not built yet"))),
    }
}

// the node key in its data folder, made on first open and readable only by its owner
fn load_key(path: &PathBuf) -> Result<NodeKey, Error> {
    if let Ok(bytes) = fs::read(path) {
        let bytes: [u8; 32] = bytes
            .try_into()
            .map_err(|_| Error::Io(format!("{} is not a node key", path.display())))?;
        return Ok(NodeKey::from_bytes(&bytes));
    }
    let key = NodeKey::generate()?;
    write_atomic(path, &key.to_bytes()).map_err(io)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o600)).map_err(io)?;
    }
    Ok(key)
}

#[cfg(test)]
mod tests {
    use crate::journal::tests::{item, put, tree};

    use super::*;

    #[tokio::test]
    async fn edits_become_a_line_of_signed_versions() {
        let dir = tempfile::tempdir().unwrap();
        let node = Node::open(&dir.path().join("data")).unwrap();
        let src = dir.path().join("src");
        let item = item(&src);
        put(&src, "a.txt", "one");
        let first = node.capture(&item).await.unwrap().unwrap();
        put(&src, "a.txt", "two");
        let second = node.capture(&item).await.unwrap().unwrap();
        assert!(first.parents.is_empty());
        assert_eq!(second.parents, vec![first.id()]);
        assert!(second.hlc > first.hlc);
        assert_eq!(first.verify(), Ok(()));
        assert_eq!(second.verify(), Ok(()));
        assert_eq!(node.heads(&item.id).unwrap(), vec![second]);
    }

    #[tokio::test]
    async fn unchanged_or_just_applied_content_commits_nothing() {
        let dir = tempfile::tempdir().unwrap();
        let node = Node::open(&dir.path().join("data")).unwrap();
        let src = dir.path().join("src");
        put(&src, "a.txt", "one");
        node.capture(&item(&src)).await.unwrap().unwrap();
        assert_eq!(node.capture(&item(&src)).await.unwrap(), None);
        // a second path for the same item id, as on another device
        let elsewhere = item(&dir.path().join("elsewhere"));
        node.apply(&elsewhere).await.unwrap();
        assert_eq!(node.capture(&elsewhere).await.unwrap(), None);
    }

    #[tokio::test]
    async fn a_copy_of_the_store_applies_byte_for_byte_with_its_own_key() {
        let dir = tempfile::tempdir().unwrap();
        let data = dir.path().join("data");
        let src = dir.path().join("src");
        put(&src, "settings.json", "{}");
        put(&src, "themes/dark.json", "dark");
        let first_id = {
            let node = Node::open(&data).unwrap();
            node.capture(&item(&src)).await.unwrap().unwrap();
            node.id()
        };
        let copy = dir.path().join("copy");
        fs::create_dir_all(&copy).unwrap();
        fs::copy(data.join("meta.db"), copy.join("meta.db")).unwrap();
        copy_tree(&data.join("blobs"), &copy.join("blobs"));
        let other = Node::open(&copy).unwrap();
        assert_ne!(other.id(), first_id);
        let target = dir.path().join("target");
        other.apply(&item(&target)).await.unwrap();
        assert_eq!(tree(&target), tree(&src));
    }

    #[test]
    fn a_data_folder_opens_once_at_a_time() {
        let dir = tempfile::tempdir().unwrap();
        let first = Node::open(dir.path()).unwrap();
        let second = Node::open(dir.path());
        assert!(matches!(second, Err(Error::Io(why)) if why.contains("in use")));
        drop(first);
        assert!(Node::open(dir.path()).is_ok());
    }

    #[test]
    fn the_key_survives_reopening() {
        let dir = tempfile::tempdir().unwrap();
        let id = Node::open(dir.path()).unwrap().id();
        assert_eq!(Node::open(dir.path()).unwrap().id(), id);
    }

    fn copy_tree(from: &Path, to: &Path) {
        fs::create_dir_all(to).unwrap();
        for entry in fs::read_dir(from).unwrap() {
            let entry = entry.unwrap();
            if entry.file_type().unwrap().is_dir() {
                copy_tree(&entry.path(), &to.join(entry.file_name()));
            } else {
                fs::copy(entry.path(), to.join(entry.file_name())).unwrap();
            }
        }
    }
}
