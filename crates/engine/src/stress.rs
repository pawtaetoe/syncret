use std::collections::HashSet;
use std::fs;
use std::path::Path;
use std::sync::Arc;

use syncret_core::{Hash, Item};
use syncret_store::{BlobStore, FsBlobs, write_atomic};

use crate::Node;
use crate::ctx::Faults;
use crate::journal::lock;
use crate::journal::tests::{item, put, tree, versions};

// the versions of an item from its single head back, which must form one line
fn line(node: &Node, item: &Item) -> Vec<Hash> {
    let heads = node.heads(&item.id).unwrap();
    assert!(heads.len() <= 1, "{} heads", heads.len());
    let mut out = Vec::new();
    let meta = lock(&node.meta);
    let mut next = heads.first().cloned();
    while let Some(version) = next {
        assert!(version.parents.len() <= 1, "not a line");
        if let Some(parent) = version.parents.first() {
            let parent = meta.get(parent).unwrap().unwrap();
            assert_ne!(
                parent.content, version.content,
                "a version repeats its parent"
            );
            next = Some(parent);
        } else {
            next = None;
        }
        out.push(version.id());
    }
    if let Some(head) = out.first() {
        assert_eq!(meta.ancestors(head).unwrap().len(), out.len());
    }
    out
}

fn temps(root: &Path) -> usize {
    let mut count = 0;
    let mut stack = vec![root.to_path_buf()];
    while let Some(dir) = stack.pop() {
        for entry in fs::read_dir(dir).unwrap() {
            let path = entry.unwrap().path();
            if path.is_dir() {
                stack.push(path);
            } else if path.to_string_lossy().ends_with(".tmp") {
                count += 1;
            }
        }
    }
    count
}

#[tokio::test(flavor = "multi_thread", worker_threads = 8)]
async fn fifty_racing_captures_leave_one_line_and_whole_blobs() {
    let dir = tempfile::tempdir().unwrap();
    let data = dir.path().join("data");
    let node = Arc::new(Node::open(&data).unwrap());
    let file = dir.path().join("CLAUDE.md");
    let item = Arc::new(item(&file));
    let mut tasks = Vec::new();
    for i in 0..50 {
        let (node, item, file) = (node.clone(), item.clone(), file.clone());
        tasks.push(tokio::spawn(async move {
            write_atomic(&file, format!("edit {}\n", i % 7).as_bytes()).unwrap();
            node.capture(&item).await.unwrap()
        }));
    }
    let made = futures_count(tasks).await;
    let versions = line(&node, &item);
    assert_eq!(versions.len(), made);
    assert!((1..=50).contains(&made));
    for id in &versions {
        let content = lock(&node.meta).get(id).unwrap().unwrap().content;
        node.blobs.get(&content).unwrap();
    }
    assert_eq!(temps(&data.join("blobs")), 0);
}

async fn futures_count(
    tasks: Vec<tokio::task::JoinHandle<Option<syncret_core::Version>>>,
) -> usize {
    let mut made = 0;
    for task in tasks {
        made += usize::from(task.await.unwrap().is_some());
    }
    made
}

#[test]
fn fifty_threads_putting_overlapping_blobs_leave_every_blob_valid() {
    let dir = tempfile::tempdir().unwrap();
    let blobs = Arc::new(FsBlobs::open(dir.path()).unwrap());
    let threads: Vec<_> = (0..50)
        .map(|t| {
            let blobs = blobs.clone();
            std::thread::spawn(move || {
                for i in 0..20 {
                    blobs
                        .put(format!("blob {}", (t + i) % 10).repeat(500).as_bytes())
                        .unwrap();
                }
            })
        })
        .collect();
    for thread in threads {
        thread.join().unwrap();
    }
    for i in 0..10 {
        let bytes = format!("blob {i}").repeat(500);
        assert_eq!(
            blobs.get(&Hash::of(bytes.as_bytes())).unwrap(),
            bytes.as_bytes()
        );
    }
    assert_eq!(temps(dir.path()), 0);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 8)]
async fn a_capture_racing_an_apply_never_commits_a_half_or_applied_tree() {
    let dir = tempfile::tempdir().unwrap();
    let node = Arc::new(Node::open(&dir.path().join("data")).unwrap());
    let (one, two) = versions(dir.path());
    let a = node.capture(&item(&one)).await.unwrap().unwrap();
    let b = node.capture(&item(&two)).await.unwrap().unwrap();
    let target = dir.path().join("target");
    for _ in 0..5 {
        node.apply_version(&item(&target), &a.id()).await.unwrap();
        let apply = {
            let (node, target, b) = (node.clone(), target.clone(), b.id());
            tokio::spawn(async move { node.apply_version(&item(&target), &b).await })
        };
        let captures: Vec<_> = (0..20)
            .map(|_| {
                let (node, target) = (node.clone(), target.clone());
                tokio::spawn(async move { node.capture(&item(&target)).await.unwrap() })
            })
            .collect();
        apply.await.unwrap().unwrap();
        for capture in captures {
            assert_eq!(capture.await.unwrap(), None);
        }
        assert_eq!(tree(&target), tree(&two));
        assert_eq!(node.heads(&item(&target).id).unwrap(), vec![b.clone()]);
    }
}

// a local edit made while a newer version waits forks instead of undoing it
#[tokio::test]
async fn an_edit_on_an_unapplied_disk_forks_rather_than_overwrites() {
    let dir = tempfile::tempdir().unwrap();
    let node = Node::open(&dir.path().join("data")).unwrap();
    let target = dir.path().join("target");
    put(&target, "a.txt", "one");
    let a = node.capture(&item(&target)).await.unwrap().unwrap();
    // a newer version arrives from elsewhere and is not applied here yet
    let elsewhere = dir.path().join("elsewhere");
    put(&elsewhere, "a.txt", "two");
    lock(&node.meta).set_local(&a).unwrap();
    let b = node.capture(&item(&elsewhere)).await.unwrap().unwrap();
    lock(&node.meta).set_local(&a).unwrap();
    assert_eq!(node.capture(&item(&target)).await.unwrap(), None);
    put(&target, "a.txt", "three");
    let c = node.capture(&item(&target)).await.unwrap().unwrap();
    assert_eq!(c.parents, vec![a.id()]);
    let heads: HashSet<_> = node
        .heads(&item(&target).id)
        .unwrap()
        .iter()
        .map(|v| v.id())
        .collect();
    assert_eq!(heads, HashSet::from([b.id(), c.id()]));
}

// an io error at every blob write of a capture leaves no version, and a retry commits once
#[tokio::test]
async fn a_failed_capture_commits_nothing_and_a_retry_commits_once() {
    let src_dir = tempfile::tempdir().unwrap();
    for i in 0..5 {
        put(src_dir.path(), &format!("f{i}.txt"), &format!("file {i}"));
    }
    let src = item(src_dir.path());
    let steps = {
        let data = tempfile::tempdir().unwrap();
        let node = Node::open(data.path())
            .unwrap()
            .with_faults(Faults::default());
        node.capture(&src).await.unwrap();
        node.faults.steps()
    };
    assert_eq!(steps, 6, "five files and the manifest");
    for step in 0..steps {
        for after in [false, true] {
            let data = tempfile::tempdir().unwrap();
            let node = Node::open(data.path())
                .unwrap()
                .with_faults(Faults::at(step, after));
            assert!(
                node.capture(&src).await.is_err(),
                "step {step} after {after}"
            );
            assert!(node.heads(&src.id).unwrap().is_empty());
            assert!(node.capture(&src).await.unwrap().is_some());
            assert_eq!(line(&node, &src).len(), 1);
        }
    }
}

// an io error at every step of an apply leaves the old tree, and a retry applies the new one
#[tokio::test]
async fn a_failed_apply_leaves_the_old_tree_and_a_retry_applies() {
    for step in 0..8 {
        for after in [false, true] {
            let dir = tempfile::tempdir().unwrap();
            let data = dir.path().join("data");
            let (one, two) = versions(dir.path());
            let target = dir.path().join("target");
            let (a, b) = {
                let node = Node::open(&data).unwrap();
                let a = node.capture(&item(&one)).await.unwrap().unwrap().id();
                let b = node.capture(&item(&two)).await.unwrap().unwrap().id();
                node.apply_version(&item(&target), &a).await.unwrap();
                (a, b)
            };
            let node = Node::open(&data)
                .unwrap()
                .with_faults(Faults::at(step, after));
            assert!(node.apply_version(&item(&target), &b).await.is_err());
            assert_eq!(tree(&target), tree(&one), "step {step} after {after}");
            assert!(lock(&node.meta).unfinished_applies().unwrap().is_empty());
            assert_eq!(
                lock(&node.meta)
                    .local(&item(&target).id)
                    .unwrap()
                    .unwrap()
                    .id(),
                a
            );
            node.apply_version(&item(&target), &b).await.unwrap();
            assert_eq!(tree(&target), tree(&two));
        }
    }
}
