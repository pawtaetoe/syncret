use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use syncret_core::recipe::parse;
use syncret_core::{Hash, Item, Os};
use syncret_engine::Node;

fn item(target: &Path) -> Item {
    let text = format!(
        "[[item]]\nname = \"x\"\nkind = \"file\"\nlinux = \"{}\"\n",
        target.display()
    );
    parse(&text).unwrap()[0]
        .resolve(Os::Linux, "pc", &|_| None)
        .unwrap()
}

// every file under root with its bytes
fn tree(root: &Path) -> Vec<(PathBuf, Vec<u8>)> {
    let mut out = Vec::new();
    let mut stack = vec![root.to_path_buf()];
    while let Some(dir) = stack.pop() {
        for entry in fs::read_dir(dir).unwrap() {
            let path = entry.unwrap().path();
            if path.is_dir() {
                stack.push(path);
            } else {
                out.push((
                    path.strip_prefix(root).unwrap().to_path_buf(),
                    fs::read(&path).unwrap(),
                ));
            }
        }
    }
    out.sort();
    out
}

// many files so a kill lands in the middle of an apply most of the time
fn version(root: &Path, salt: &str, files: usize) {
    for i in 0..files {
        let path = root.join(format!("d{}/f{i}.txt", i % 10));
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, format!("{salt} {i} ").repeat(200)).unwrap();
    }
}

fn hash(hex: &str) -> Hash {
    let bytes: Vec<u8> = (0..64)
        .step_by(2)
        .map(|i| u8::from_str_radix(&hex[i..i + 2], 16).unwrap())
        .collect();
    Hash(bytes.try_into().unwrap())
}

fn jitter_ms(max: u64) -> u64 {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .subsec_nanos() as u64;
    5 + nanos % max
}

// the child side, it applies the two versions back and forth until it is killed
#[tokio::test]
#[ignore]
async fn kill_child() {
    let Ok(data) = std::env::var("SYNCRET_KILL_DATA") else {
        return;
    };
    let target = std::env::var("SYNCRET_KILL_TARGET").unwrap();
    let a = hash(&std::env::var("SYNCRET_KILL_A").unwrap());
    let b = hash(&std::env::var("SYNCRET_KILL_B").unwrap());
    let node = Node::open(Path::new(&data)).unwrap();
    let item = item(Path::new(&target));
    loop {
        node.apply_version(&item, &b).await.unwrap();
        node.apply_version(&item, &a).await.unwrap();
    }
}

// spec criterion 5 with a real kill, the target is always the old or the new version
#[tokio::test]
#[ignore]
async fn an_apply_killed_at_random_recovers_to_one_whole_version() {
    let dir = tempfile::tempdir().unwrap();
    let data = dir.path().join("data");
    let (src_a, src_b, target) = (
        dir.path().join("a"),
        dir.path().join("b"),
        dir.path().join("t"),
    );
    version(&src_a, "alpha", 300);
    version(&src_b, "beta", 240);
    let (a, b, window) = {
        let node = Node::open(&data).unwrap();
        let a = node.capture(&item(&src_a)).await.unwrap().unwrap().id();
        let b = node.capture(&item(&src_b)).await.unwrap().unwrap().id();
        let started = std::time::Instant::now();
        node.apply_version(&item(&target), &a).await.unwrap();
        // kills spread over about two and a half applies, so they land in either one
        let window = started.elapsed().as_millis() as u64 * 5 / 2 + 100;
        (a, b, window)
    };
    let (want_a, want_b) = (tree(&src_a), tree(&src_b));
    let mut landed = [0, 0];
    for round in 0..20 {
        let mut child = Command::new(std::env::current_exe().unwrap())
            .args(["--exact", "kill_child", "--ignored", "--nocapture"])
            .env("SYNCRET_KILL_DATA", &data)
            .env("SYNCRET_KILL_TARGET", &target)
            .env("SYNCRET_KILL_A", a.to_string())
            .env("SYNCRET_KILL_B", b.to_string())
            .spawn()
            .unwrap();
        std::thread::sleep(Duration::from_millis(jitter_ms(window)));
        child.kill().unwrap();
        child.wait().unwrap();
        drop(Node::open(&data).unwrap());
        let now = tree(&target);
        if now != want_a && now != want_b {
            let odd: Vec<_> = now
                .iter()
                .filter(|f| !want_a.contains(f) && !want_b.contains(f))
                .map(|f| f.0.display().to_string())
                .collect();
            panic!(
                "round {round} left a mix of versions, {} files from neither, such as {:?}",
                odd.len(),
                &odd[..odd.len().min(5)]
            );
        }
        landed[usize::from(now == want_b)] += 1;
    }
    eprintln!(
        "ended on the first version {} times, the second {} times",
        landed[0], landed[1]
    );
}

// the child side of the lock test, it holds a node open until it is killed
#[test]
#[ignore]
fn hold_child() {
    let Ok(data) = std::env::var("SYNCRET_HOLD_DATA") else {
        return;
    };
    let _node = Node::open(Path::new(&data)).unwrap();
    fs::write(Path::new(&data).join("held"), b"").unwrap();
    std::thread::sleep(Duration::from_secs(600));
}

// a second process is refused while a node is open, and gets in once the first is killed
#[test]
#[ignore]
fn a_second_process_is_refused_until_the_first_is_killed() {
    let dir = tempfile::tempdir().unwrap();
    let mut child = Command::new(std::env::current_exe().unwrap())
        .args(["--exact", "hold_child", "--ignored", "--nocapture"])
        .env("SYNCRET_HOLD_DATA", dir.path())
        .spawn()
        .unwrap();
    while !dir.path().join("held").exists() {
        std::thread::sleep(Duration::from_millis(20));
    }
    let refused = Node::open(dir.path()).err().expect("the folder is held");
    assert!(refused.to_string().contains("in use"), "{refused}");
    child.kill().unwrap();
    child.wait().unwrap();
    assert!(Node::open(dir.path()).is_ok());
}
