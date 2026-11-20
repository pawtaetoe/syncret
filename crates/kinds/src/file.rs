use std::collections::HashSet;
use std::fs;
use std::io::ErrorKind;
use std::path::{Path, PathBuf};

use async_trait::async_trait;
use globset::{Glob, GlobSet, GlobSetBuilder};
use syncret_core::{Ctx, Error, Hash, Kind, Merged, Snapshot};

use crate::{Entry, EntryKind, Manifest};

// files and folders synced byte for byte, as dotfiles and editor configs are
pub struct FileKind;

#[async_trait]
impl Kind for FileKind {
    fn id(&self) -> &'static str {
        "file"
    }

    fn sensitive(&self) -> bool {
        false
    }

    async fn capture(&self, ctx: &dyn Ctx) -> Result<Snapshot, Error> {
        let root = root(ctx)?;
        let ignore = ignore_set(&ctx.item().ignore)?;
        let mut entries = Vec::new();
        match fs::metadata(root) {
            Ok(meta) if meta.is_dir() => walk(ctx, root, "", &ignore, &mut entries)?,
            Ok(_) => entries.push(entry(ctx, root, String::new())?.expect("a file")),
            Err(e) if e.kind() == ErrorKind::NotFound => {}
            Err(e) => return Err(io(e)),
        }
        let manifest = Manifest::new(entries);
        Ok(Snapshot {
            content: ctx.put_blob(&manifest.encode())?,
            meta: Vec::new(),
        })
    }

    async fn merge(
        &self,
        _ctx: &dyn Ctx,
        _base: Option<&Snapshot>,
        _ours: &Snapshot,
        _theirs: &Snapshot,
    ) -> Result<Merged, Error> {
        Err(Error::Io("file merges arrive in phase 3".into()))
    }

    fn validate(&self, ctx: &dyn Ctx, merged: &Merged) -> Result<(), Error> {
        Manifest::decode(&ctx.get_blob(&merged.content)?).map(|_| ())
    }

    async fn apply(&self, ctx: &dyn Ctx, merged: &Merged) -> Result<(), Error> {
        let root = root(ctx)?;
        let manifest = Manifest::decode(&ctx.get_blob(&merged.content)?)?;
        let ignore = ignore_set(&ctx.item().ignore)?;
        let single = manifest.entries.len() == 1 && manifest.entries[0].path.is_empty();
        // stale files go first, so a folder that becomes a file has room
        if !single && fs::metadata(root).is_ok_and(|m| m.is_dir()) {
            let wanted: HashSet<&str> = manifest.entries.iter().map(|e| e.path.as_str()).collect();
            let mut present = Vec::new();
            walk_paths(root, "", &ignore, &mut present)?;
            for (rel, is_dir) in present.iter().rev() {
                let path = join(root, rel);
                if *is_dir {
                    let empty = fs::read_dir(&path).map_err(io)?.next().is_none();
                    if empty && !wanted.iter().any(|w| w.starts_with(&format!("{rel}/"))) {
                        ctx.remove(&path)?;
                    }
                } else if !wanted.contains(rel.as_str()) {
                    ctx.remove(&path)?;
                }
            }
        } else if manifest.entries.is_empty()
            && fs::symlink_metadata(root).is_ok_and(|m| !m.is_dir())
        {
            ctx.remove(root)?;
        }
        for entry in &manifest.entries {
            let path = join(root, &entry.path);
            match entry.kind {
                EntryKind::File => {
                    if unchanged(&path, entry) {
                        continue;
                    }
                    ctx.write(&path, &ctx.get_blob(&entry.hash)?, Some(entry.mode))?;
                }
                EntryKind::Link => {
                    let target = String::from_utf8(ctx.get_blob(&entry.hash)?)
                        .map_err(|_| Error::Io(format!("link {} is not utf-8", entry.path)))?;
                    if fs::read_link(&path).is_ok_and(|t| t == Path::new(&target)) {
                        continue;
                    }
                    ctx.link(&path, Path::new(&target))?;
                }
            }
        }
        Ok(())
    }
}

fn root(ctx: &dyn Ctx) -> Result<&Path, Error> {
    ctx.item()
        .path
        .as_deref()
        .ok_or_else(|| Error::Recipe(format!("{} has no path here", ctx.item().id)))
}

fn ignore_set(patterns: &[String]) -> Result<GlobSet, Error> {
    let mut set = GlobSetBuilder::new();
    for pattern in patterns {
        set.add(Glob::new(pattern).map_err(|e| Error::Recipe(e.to_string()))?);
    }
    set.build().map_err(|e| Error::Recipe(e.to_string()))
}

fn io(e: std::io::Error) -> Error {
    Error::Io(e.to_string())
}

fn join(root: &Path, rel: &str) -> PathBuf {
    if rel.is_empty() {
        return root.to_path_buf();
    }
    rel.split('/')
        .fold(root.to_path_buf(), |path, part| path.join(part))
}

// the relative paths under folder that are not ignored, folders flagged, never following links
fn walk_paths(
    root: &Path,
    rel: &str,
    ignore: &GlobSet,
    out: &mut Vec<(String, bool)>,
) -> Result<(), Error> {
    let mut children: Vec<_> = fs::read_dir(join(root, rel))
        .map_err(io)?
        .collect::<Result<_, _>>()
        .map_err(io)?;
    children.sort_by_key(|c| c.file_name());
    for child in children {
        // a name that is not utf-8 cannot travel to every system, so it is left out
        let Some(name) = child.file_name().to_str().map(str::to_owned) else {
            continue;
        };
        let path = if rel.is_empty() {
            name
        } else {
            format!("{rel}/{name}")
        };
        if ignore.is_match(&path) {
            continue;
        }
        let is_dir = child.file_type().map_err(io)?.is_dir();
        out.push((path.clone(), is_dir));
        if is_dir {
            walk_paths(root, &path, ignore, out)?;
        }
    }
    Ok(())
}

fn walk(
    ctx: &dyn Ctx,
    root: &Path,
    rel: &str,
    ignore: &GlobSet,
    out: &mut Vec<Entry>,
) -> Result<(), Error> {
    let mut paths = Vec::new();
    walk_paths(root, rel, ignore, &mut paths)?;
    for (path, is_dir) in paths {
        if !is_dir && let Some(entry) = entry(ctx, &join(root, &path), path)? {
            out.push(entry);
        }
    }
    Ok(())
}

// a file or link as an entry with its content stored, anything else is skipped
fn entry(ctx: &dyn Ctx, path: &Path, rel: String) -> Result<Option<Entry>, Error> {
    let meta = fs::symlink_metadata(path).map_err(io)?;
    if meta.file_type().is_symlink() {
        let target = fs::read_link(path).map_err(io)?;
        let target = target
            .to_str()
            .ok_or_else(|| Error::Io(format!("link {rel} is not utf-8")))?;
        return Ok(Some(Entry {
            path: rel,
            kind: EntryKind::Link,
            mode: 0o777,
            hash: ctx.put_blob(target.as_bytes())?,
        }));
    }
    if !meta.is_file() {
        return Ok(None);
    }
    Ok(Some(Entry {
        path: rel,
        kind: EntryKind::File,
        mode: mode(&meta),
        hash: ctx.put_blob(&fs::read(path).map_err(io)?)?,
    }))
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

// true when path already holds this file with this mode, so it is not rewritten
fn unchanged(path: &Path, entry: &Entry) -> bool {
    let Ok(meta) = fs::symlink_metadata(path) else {
        return false;
    };
    meta.is_file()
        && mode(&meta) == entry.mode
        && fs::read(path).is_ok_and(|bytes| Hash::of(&bytes) == entry.hash)
}

#[cfg(test)]
mod tests {
    use std::os::unix::fs::{PermissionsExt, symlink};

    use syncret_core::recipe::parse;
    use syncret_core::{Item, Os};
    use syncret_store::{BlobStore, FsBlobs, write_atomic};

    use super::*;

    // a ctx writing straight to disk, the engine adds the journal
    struct TestCtx {
        item: Item,
        blobs: FsBlobs,
    }

    impl TestCtx {
        fn new(data: &Path, target: &Path) -> Self {
            let text = format!(
                "[[item]]\nname = \"x\"\nkind = \"file\"\nlinux = \"{}\"\nignore = [\"*.log\"]\n",
                target.display()
            );
            let item = parse(&text).unwrap()[0]
                .resolve(Os::Linux, "pc", &|_| None)
                .unwrap();
            Self {
                item,
                blobs: FsBlobs::open(data).unwrap(),
            }
        }
    }

    impl Ctx for TestCtx {
        fn item(&self) -> &Item {
            &self.item
        }
        fn put_blob(&self, bytes: &[u8]) -> Result<Hash, Error> {
            self.blobs.put(bytes).map_err(|e| Error::Io(e.to_string()))
        }
        fn get_blob(&self, hash: &Hash) -> Result<Vec<u8>, Error> {
            self.blobs.get(hash).map_err(|e| Error::Io(e.to_string()))
        }
        fn write(&self, path: &Path, bytes: &[u8], mode: Option<u32>) -> Result<(), Error> {
            write_atomic(path, bytes).map_err(io)?;
            if let Some(mode) = mode {
                fs::set_permissions(path, fs::Permissions::from_mode(mode)).map_err(io)?;
            }
            Ok(())
        }
        fn remove(&self, path: &Path) -> Result<(), Error> {
            if fs::symlink_metadata(path).map_err(io)?.is_dir() {
                fs::remove_dir(path).map_err(io)
            } else {
                fs::remove_file(path).map_err(io)
            }
        }
        fn link(&self, path: &Path, target: &Path) -> Result<(), Error> {
            let _ = fs::remove_file(path);
            fs::create_dir_all(path.parent().unwrap()).map_err(io)?;
            symlink(target, path).map_err(io)
        }
    }

    fn put(root: &Path, rel: &str, text: &str) {
        let path = root.join(rel);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, text).unwrap();
    }

    // every file under a folder with its bytes and mode, links as their targets
    fn tree(root: &Path) -> Vec<(String, Vec<u8>, u32)> {
        let mut out = Vec::new();
        let mut paths = Vec::new();
        walk_paths(root, "", &GlobSet::empty(), &mut paths).unwrap();
        for (rel, is_dir) in paths {
            let path = join(root, &rel);
            let meta = fs::symlink_metadata(&path).unwrap();
            if meta.file_type().is_symlink() {
                let target = fs::read_link(&path).unwrap();
                out.push((rel, target.to_string_lossy().as_bytes().to_vec(), 0));
            } else if !is_dir {
                out.push((rel, fs::read(&path).unwrap(), mode(&meta)));
            }
        }
        out
    }

    fn manifest(ctx: &TestCtx, snapshot: &Snapshot) -> Manifest {
        Manifest::decode(&ctx.get_blob(&snapshot.content).unwrap()).unwrap()
    }

    #[tokio::test]
    async fn the_same_tree_gives_the_same_hash_in_any_order() {
        let data = tempfile::tempdir().unwrap();
        let a = tempfile::tempdir().unwrap();
        let b = tempfile::tempdir().unwrap();
        for rel in ["x", "d/y", "d/e/z"] {
            put(a.path(), rel, rel);
        }
        for rel in ["d/e/z", "x", "d/y"] {
            put(b.path(), rel, rel);
        }
        let one = FileKind
            .capture(&TestCtx::new(data.path(), a.path()))
            .await
            .unwrap();
        let two = FileKind
            .capture(&TestCtx::new(data.path(), b.path()))
            .await
            .unwrap();
        let again = FileKind
            .capture(&TestCtx::new(data.path(), a.path()))
            .await
            .unwrap();
        assert_eq!(one.content, two.content);
        assert_eq!(one, again);
    }

    #[tokio::test]
    async fn ignored_files_are_left_out_and_links_are_not_followed() {
        let data = tempfile::tempdir().unwrap();
        let src = tempfile::tempdir().unwrap();
        let outside = tempfile::tempdir().unwrap();
        put(outside.path(), "secret", "do not read me");
        put(src.path(), "keep.txt", "kept");
        put(src.path(), "logs/run.log", "noise");
        symlink(outside.path().join("secret"), src.path().join("link")).unwrap();
        let ctx = TestCtx::new(data.path(), src.path());
        let snapshot = FileKind.capture(&ctx).await.unwrap();
        let entries = manifest(&ctx, &snapshot).entries;
        let paths: Vec<_> = entries.iter().map(|e| e.path.as_str()).collect();
        assert_eq!(paths, ["keep.txt", "link"]);
        assert_eq!(entries[1].kind, EntryKind::Link);
        assert!(!ctx.blobs.has(&Hash::of(b"do not read me")));
    }

    #[tokio::test]
    async fn modes_are_kept() {
        let data = tempfile::tempdir().unwrap();
        let src = tempfile::tempdir().unwrap();
        put(src.path(), "run.sh", "#!/bin/sh\n");
        fs::set_permissions(src.path().join("run.sh"), fs::Permissions::from_mode(0o755)).unwrap();
        let ctx = TestCtx::new(data.path(), src.path());
        let snapshot = FileKind.capture(&ctx).await.unwrap();
        assert_eq!(manifest(&ctx, &snapshot).entries[0].mode, 0o755);
    }

    #[tokio::test]
    async fn a_folder_round_trips_byte_for_byte() {
        let data = tempfile::tempdir().unwrap();
        let src = tempfile::tempdir().unwrap();
        let dst = tempfile::tempdir().unwrap();
        put(src.path(), "settings.json", "{\"a\": 1}\n");
        put(src.path(), "themes/dark/theme.json", "dark");
        put(src.path(), "bin/tool", "\0\x01\x02 binary");
        fs::set_permissions(
            src.path().join("bin/tool"),
            fs::Permissions::from_mode(0o700),
        )
        .unwrap();
        symlink("settings.json", src.path().join("current")).unwrap();
        let snapshot = FileKind
            .capture(&TestCtx::new(data.path(), src.path()))
            .await
            .unwrap();
        let target = TestCtx::new(data.path(), &dst.path().join("zed"));
        FileKind.validate(&target, &snapshot).unwrap();
        FileKind.apply(&target, &snapshot).await.unwrap();
        assert_eq!(tree(src.path()), tree(&dst.path().join("zed")));
    }

    #[tokio::test]
    async fn a_single_file_round_trips() {
        let data = tempfile::tempdir().unwrap();
        let dir = tempfile::tempdir().unwrap();
        put(dir.path(), "CLAUDE.md", "# rules\n");
        let snapshot = FileKind
            .capture(&TestCtx::new(data.path(), &dir.path().join("CLAUDE.md")))
            .await
            .unwrap();
        let out = dir.path().join("copy/CLAUDE.md");
        FileKind
            .apply(&TestCtx::new(data.path(), &out), &snapshot)
            .await
            .unwrap();
        assert_eq!(fs::read(out).unwrap(), b"# rules\n");
    }

    #[tokio::test]
    async fn applying_over_a_tree_matches_it_and_leaves_ignored_and_outside_files() {
        let data = tempfile::tempdir().unwrap();
        let src = tempfile::tempdir().unwrap();
        let dst = tempfile::tempdir().unwrap();
        let item = dst.path().join("item");
        put(src.path(), "a.txt", "new a");
        put(src.path(), "sub", "was a folder, now a file");
        put(&item, "a.txt", "old a");
        put(&item, "stale.txt", "gone");
        put(&item, "sub/inner.txt", "gone too");
        put(&item, "debug.log", "ignored, stays");
        put(dst.path(), "beside.txt", "outside the item");
        let snapshot = FileKind
            .capture(&TestCtx::new(data.path(), src.path()))
            .await
            .unwrap();
        FileKind
            .apply(&TestCtx::new(data.path(), &item), &snapshot)
            .await
            .unwrap();
        assert_eq!(fs::read_to_string(item.join("a.txt")).unwrap(), "new a");
        assert_eq!(
            fs::read_to_string(item.join("sub")).unwrap(),
            "was a folder, now a file"
        );
        assert!(!item.join("stale.txt").exists());
        assert_eq!(
            fs::read_to_string(item.join("debug.log")).unwrap(),
            "ignored, stays"
        );
        assert_eq!(
            fs::read_to_string(dst.path().join("beside.txt")).unwrap(),
            "outside the item"
        );
    }

    #[tokio::test]
    async fn a_manifest_that_escapes_the_item_is_refused() {
        let data = tempfile::tempdir().unwrap();
        let dst = tempfile::tempdir().unwrap();
        let ctx = TestCtx::new(data.path(), &dst.path().join("item"));
        let evil = Manifest {
            entries: vec![Entry {
                path: "../escaped".into(),
                kind: EntryKind::File,
                mode: 0o644,
                hash: ctx.put_blob(b"owned").unwrap(),
            }],
        };
        let snapshot = Snapshot {
            content: ctx.put_blob(&evil.encode()).unwrap(),
            meta: Vec::new(),
        };
        assert!(FileKind.validate(&ctx, &snapshot).is_err());
        assert!(FileKind.apply(&ctx, &snapshot).await.is_err());
        assert!(!dst.path().join("escaped").exists());
    }
}
