use std::path::Path;
use std::sync::Mutex;

use syncret_core::{Error, Item, Kind, Merged};
use syncret_store::{FsBlobs, Meta};

use crate::ctx::{Faults, NodeCtx, restore, store};

// apply merged through the undo journal, rolling back on any error
pub(crate) async fn apply(
    kind: &dyn Kind,
    item: &Item,
    blobs: &FsBlobs,
    meta: &Mutex<Meta>,
    merged: &Merged,
    faults: &Faults,
) -> Result<(), Error> {
    let apply = lock(meta).begin_apply(&item.id).map_err(store)?;
    let result = {
        let ctx = NodeCtx::journaled(item, blobs, meta, apply, faults);
        kind.validate(&ctx, merged)?;
        kind.apply(&ctx, merged).await
    };
    match result {
        Ok(()) => lock(meta).finish_apply(apply).map_err(store),
        Err(e) => {
            recover(blobs, meta)?;
            Err(e)
        }
    }
}

// roll every unfinished apply back to what it replaced, safe to run again
pub fn recover(blobs: &FsBlobs, meta: &Mutex<Meta>) -> Result<(), Error> {
    let unfinished = lock(meta).unfinished_applies().map_err(store)?;
    for (apply, undo) in unfinished {
        for step in undo {
            restore(blobs, Path::new(&step.path), &step.before)?;
        }
        lock(meta).finish_apply(apply).map_err(store)?;
    }
    Ok(())
}

pub(crate) fn lock(meta: &Mutex<Meta>) -> std::sync::MutexGuard<'_, Meta> {
    meta.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
}

#[cfg(test)]
pub(crate) mod tests {
    use std::fs;
    use std::os::unix::fs::{PermissionsExt, symlink};
    use std::path::Path;

    use syncret_core::recipe::parse;
    use syncret_core::{Kind, Os, Snapshot};
    use syncret_kinds::FileKind;

    use super::*;

    pub(crate) fn item(target: &Path) -> Item {
        let text = format!(
            "[[item]]\nname = \"x\"\nkind = \"file\"\nlinux = \"{}\"\nignore = [\"*.log\"]\n",
            target.display()
        );
        parse(&text).unwrap()[0]
            .resolve(Os::Linux, "pc", &|_| None)
            .unwrap()
    }

    pub(crate) fn put(root: &Path, rel: &str, text: &str) {
        let path = root.join(rel);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, text).unwrap();
    }

    // every entry under root with its bytes, mode or link target, folders included
    pub(crate) fn tree(root: &Path) -> Vec<(String, String)> {
        let mut out = Vec::new();
        fn visit(root: &Path, path: &Path, out: &mut Vec<(String, String)>) {
            let rel = path
                .strip_prefix(root)
                .unwrap()
                .to_string_lossy()
                .into_owned();
            let meta = fs::symlink_metadata(path).unwrap();
            if meta.file_type().is_symlink() {
                out.push((
                    rel,
                    format!("link {}", fs::read_link(path).unwrap().display()),
                ));
            } else if meta.is_dir() {
                out.push((rel, "folder".into()));
                let mut children: Vec<_> = fs::read_dir(path)
                    .unwrap()
                    .map(|c| c.unwrap().path())
                    .collect();
                children.sort();
                for child in children {
                    visit(root, &child, out);
                }
            } else {
                let mode = meta.permissions().mode() & 0o777;
                out.push((
                    rel,
                    format!(
                        "{mode:o} {}",
                        String::from_utf8_lossy(&fs::read(path).unwrap())
                    ),
                ));
            }
        }
        if root.exists() {
            visit(root, root, &mut out);
        }
        out
    }

    pub(crate) async fn capture(blobs: &FsBlobs, src: &Path) -> Snapshot {
        let item = item(src);
        let faults = Faults::default();
        FileKind
            .capture(&NodeCtx::reading(&item, blobs, &faults))
            .await
            .unwrap()
    }

    // two versions that differ in every way an apply can change a tree
    pub(crate) fn versions(dir: &Path) -> (std::path::PathBuf, std::path::PathBuf) {
        let one = dir.join("one");
        let two = dir.join("two");
        put(&one, "same.txt", "unchanged");
        put(&one, "edit.txt", "before");
        put(&one, "gone.txt", "removed later");
        put(&one, "swap/inner.txt", "a folder that becomes a file");
        put(&one, "deep/a/b.txt", "deep");
        symlink("same.txt", one.join("link")).unwrap();
        put(&two, "same.txt", "unchanged");
        put(&two, "edit.txt", "after");
        put(&two, "swap", "now a file");
        put(&two, "deep/a/b.txt", "deep");
        put(&two, "new/nested/c.txt", "new");
        symlink("edit.txt", two.join("link")).unwrap();
        fs::set_permissions(two.join("same.txt"), fs::Permissions::from_mode(0o600)).unwrap();
        (one, two)
    }

    struct Setup {
        _dir: tempfile::TempDir,
        blobs: FsBlobs,
        meta: Mutex<Meta>,
        target: Item,
        one: Snapshot,
        two: Snapshot,
        expected_one: Vec<(String, String)>,
        expected_two: Vec<(String, String)>,
    }

    async fn setup() -> Setup {
        let dir = tempfile::tempdir().unwrap();
        let blobs = FsBlobs::open(&dir.path().join("data")).unwrap();
        let meta = Mutex::new(Meta::open(&dir.path().join("data")).unwrap());
        let (one, two) = versions(dir.path());
        let one_snap = capture(&blobs, &one).await;
        let two_snap = capture(&blobs, &two).await;
        let target = item(&dir.path().join("target"));
        let none = Faults::default();
        apply(&FileKind, &target, &blobs, &meta, &one_snap, &none)
            .await
            .unwrap();
        let expected_one = tree(&one);
        let expected_two = tree(&two);
        assert_eq!(tree(target.path.as_ref().unwrap()), expected_one);
        Setup {
            _dir: dir,
            blobs,
            meta,
            target,
            one: one_snap,
            two: two_snap,
            expected_one,
            expected_two,
        }
    }

    #[tokio::test]
    async fn a_clean_apply_gives_the_new_version() {
        let s = setup().await;
        let none = Faults::default();
        apply(&FileKind, &s.target, &s.blobs, &s.meta, &s.two, &none)
            .await
            .unwrap();
        assert_eq!(tree(s.target.path.as_ref().unwrap()), s.expected_two);
        recover(&s.blobs, &s.meta).unwrap();
        assert_eq!(tree(s.target.path.as_ref().unwrap()), s.expected_two);
    }

    // a crash at every step, before and after it acts, recovers to the old version
    #[tokio::test]
    async fn a_crash_at_any_step_recovers_to_the_old_version() {
        let s = setup().await;
        let count = Faults::default();
        let apply_id = lock(&s.meta).begin_apply("probe").unwrap();
        {
            // a dry count of the steps on a copy of the target
            let probe_dir = tempfile::tempdir().unwrap();
            let probe = item(&probe_dir.path().join("t"));
            let none = Faults::default();
            apply(&FileKind, &probe, &s.blobs, &s.meta, &s.one, &none)
                .await
                .unwrap();
            let ctx = NodeCtx::journaled(&probe, &s.blobs, &s.meta, apply_id, &count);
            FileKind.apply(&ctx, &s.two).await.unwrap();
        }
        lock(&s.meta).finish_apply(apply_id).unwrap();
        let steps = count.steps();
        assert_eq!(steps, 8, "3 removes, 4 writes and 1 link");
        let target = s.target.path.as_ref().unwrap();
        for step in 0..steps {
            for after in [false, true] {
                let faults = Faults::at(step, after);
                let apply_id = lock(&s.meta).begin_apply(&s.target.id).unwrap();
                let ctx = NodeCtx::journaled(&s.target, &s.blobs, &s.meta, apply_id, &faults);
                // the process dies here, nothing finishes or rolls back in it
                assert!(FileKind.apply(&ctx, &s.two).await.is_err());
                recover(&s.blobs, &s.meta).unwrap();
                assert_eq!(tree(target), s.expected_one, "step {step} after {after}");
            }
        }
    }

    #[tokio::test]
    async fn an_error_during_apply_rolls_back_at_once() {
        let s = setup().await;
        let faults = Faults::at(3, true);
        let result = apply(&FileKind, &s.target, &s.blobs, &s.meta, &s.two, &faults).await;
        assert!(result.is_err());
        assert_eq!(tree(s.target.path.as_ref().unwrap()), s.expected_one);
        assert!(lock(&s.meta).unfinished_applies().unwrap().is_empty());
    }

    #[tokio::test]
    async fn recovery_is_idempotent() {
        let s = setup().await;
        let faults = Faults::at(5, false);
        let apply_id = lock(&s.meta).begin_apply(&s.target.id).unwrap();
        let ctx = NodeCtx::journaled(&s.target, &s.blobs, &s.meta, apply_id, &faults);
        let _ = FileKind.apply(&ctx, &s.two).await;
        // replay the rollback steps by hand before recovery, as if recovery itself crashed
        let undo = lock(&s.meta).unfinished_applies().unwrap();
        for step in undo[0].1.iter().take(2) {
            restore(&s.blobs, Path::new(&step.path), &step.before).unwrap();
        }
        recover(&s.blobs, &s.meta).unwrap();
        recover(&s.blobs, &s.meta).unwrap();
        assert_eq!(tree(s.target.path.as_ref().unwrap()), s.expected_one);
    }

    #[tokio::test]
    async fn writes_outside_an_apply_are_refused() {
        let s = setup().await;
        let faults = Faults::default();
        let ctx = NodeCtx::reading(&s.target, &s.blobs, &faults);
        let path = s.target.path.as_ref().unwrap().join("x");
        assert!(syncret_core::Ctx::write(&ctx, &path, b"x", None).is_err());
        assert!(!path.exists());
    }
}
