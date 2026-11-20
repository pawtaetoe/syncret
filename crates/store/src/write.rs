use std::fs::{self, File};
use std::io::Write;
use std::path::Path;
use std::process;
use std::sync::atomic::{AtomicU64, Ordering};

// unique temp names within this process, the folder lock keeps other processes out
static NEXT: AtomicU64 = AtomicU64::new(0);

// write bytes to path through a temp file in the same folder, fsync, rename
pub fn write_atomic(path: &Path, bytes: &[u8]) -> std::io::Result<()> {
    let folder = path.parent().unwrap_or(Path::new("."));
    fs::create_dir_all(folder)?;
    let name = path.file_name().unwrap_or_default().to_string_lossy();
    let temp = folder.join(format!(
        ".{name}.{}.{}.tmp",
        process::id(),
        NEXT.fetch_add(1, Ordering::Relaxed)
    ));
    let written = (|| {
        let mut file = File::create(&temp)?;
        file.write_all(bytes)?;
        file.sync_all()?;
        fs::rename(&temp, path)
    })();
    if written.is_err() {
        let _ = fs::remove_file(&temp);
    }
    written?;
    sync_folder(folder)
}

// remove temp files a killed write_atomic left beside path
pub fn clear_temps(path: &Path) -> std::io::Result<()> {
    let (Some(folder), Some(name)) = (path.parent(), path.file_name()) else {
        return Ok(());
    };
    let prefix = format!(".{}.", name.to_string_lossy());
    let entries = match fs::read_dir(folder) {
        Ok(entries) => entries,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(e) => return Err(e),
    };
    for entry in entries {
        let entry = entry?;
        let file = entry.file_name().to_string_lossy().into_owned();
        if file.starts_with(&prefix) && file.ends_with(".tmp") {
            fs::remove_file(entry.path())?;
        }
    }
    Ok(())
}

// make a rename durable, which on unix needs the folder itself synced
#[cfg(unix)]
pub(crate) fn sync_folder(folder: &Path) -> std::io::Result<()> {
    File::open(folder)?.sync_all()
}

#[cfg(not(unix))]
pub(crate) fn sync_folder(_folder: &Path) -> std::io::Result<()> {
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn clear_temps_removes_only_temps_of_that_file() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("a.txt");
        fs::write(&path, b"real").unwrap();
        fs::write(dir.path().join(".a.txt.1.0.tmp"), b"half").unwrap();
        fs::write(dir.path().join(".b.txt.1.0.tmp"), b"other file").unwrap();
        fs::write(dir.path().join(".a.txt.keep"), b"not a temp").unwrap();
        clear_temps(&path).unwrap();
        let mut left: Vec<_> = fs::read_dir(dir.path())
            .unwrap()
            .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
            .collect();
        left.sort();
        assert_eq!(left, [".a.txt.keep", ".b.txt.1.0.tmp", "a.txt"]);
        clear_temps(&dir.path().join("missing/x")).unwrap();
    }
}
