use std::fs;
use std::io::ErrorKind;
use std::path::{Path, PathBuf};

use syncret_core::Hash;

use crate::{Error, write_atomic};

// content-addressed bytes, so phase 2 can put iroh behind the same calls
pub trait BlobStore: Send + Sync {
    fn put(&self, bytes: &[u8]) -> Result<Hash, Error>;
    fn get(&self, hash: &Hash) -> Result<Vec<u8>, Error>;
    fn has(&self, hash: &Hash) -> bool;
}

// blobs as files named by their hash under a data folder
pub struct FsBlobs {
    root: PathBuf,
}

impl FsBlobs {
    pub fn open(data: &Path) -> Result<Self, Error> {
        let root = data.join("blobs");
        fs::create_dir_all(&root)?;
        // temp files from writes killed part way are never valid blobs
        for folder in fs::read_dir(&root)? {
            for file in fs::read_dir(folder?.path())? {
                let file = file?;
                if file.file_name().to_string_lossy().ends_with(".tmp") {
                    fs::remove_file(file.path())?;
                }
            }
        }
        Ok(Self { root })
    }

    fn path(&self, hash: &Hash) -> PathBuf {
        let name = hash.to_string();
        self.root.join(&name[..2]).join(&name[2..])
    }
}

impl BlobStore for FsBlobs {
    fn put(&self, bytes: &[u8]) -> Result<Hash, Error> {
        let hash = Hash::of(bytes);
        // an existing copy is kept only when it still matches, so a damaged one is replaced
        if self.get(&hash).is_err() {
            write_atomic(&self.path(&hash), bytes)?;
        }
        Ok(hash)
    }

    fn get(&self, hash: &Hash) -> Result<Vec<u8>, Error> {
        let bytes = match fs::read(self.path(hash)) {
            Ok(bytes) => bytes,
            Err(e) if e.kind() == ErrorKind::NotFound => return Err(Error::Missing(*hash)),
            Err(e) => return Err(e.into()),
        };
        if Hash::of(&bytes) != *hash {
            return Err(Error::Corrupt(*hash));
        }
        Ok(bytes)
    }

    fn has(&self, hash: &Hash) -> bool {
        self.path(hash).is_file()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn files(root: &Path) -> usize {
        fs::read_dir(root.join("blobs"))
            .unwrap()
            .flat_map(|d| fs::read_dir(d.unwrap().path()).unwrap())
            .count()
    }

    #[test]
    fn put_then_get_returns_the_same_bytes() {
        let dir = tempfile::tempdir().unwrap();
        let blobs = FsBlobs::open(dir.path()).unwrap();
        let hash = blobs.put(b"hello").unwrap();
        assert_eq!(hash, Hash::of(b"hello"));
        assert_eq!(blobs.get(&hash).unwrap(), b"hello");
        assert!(blobs.has(&hash));
    }

    #[test]
    fn the_same_bytes_are_stored_once() {
        let dir = tempfile::tempdir().unwrap();
        let blobs = FsBlobs::open(dir.path()).unwrap();
        blobs.put(b"same").unwrap();
        blobs.put(b"same").unwrap();
        blobs.put(b"other").unwrap();
        assert_eq!(files(dir.path()), 2);
    }

    #[test]
    fn a_damaged_blob_is_reported_never_returned_and_put_repairs_it() {
        let dir = tempfile::tempdir().unwrap();
        let blobs = FsBlobs::open(dir.path()).unwrap();
        let hash = blobs.put(b"original").unwrap();
        fs::write(blobs.path(&hash), b"tampered").unwrap();
        assert!(matches!(blobs.get(&hash), Err(Error::Corrupt(h)) if h == hash));
        blobs.put(b"original").unwrap();
        assert_eq!(blobs.get(&hash).unwrap(), b"original");
    }

    #[test]
    fn a_missing_blob_is_reported() {
        let dir = tempfile::tempdir().unwrap();
        let blobs = FsBlobs::open(dir.path()).unwrap();
        let hash = Hash::of(b"never stored");
        assert!(matches!(blobs.get(&hash), Err(Error::Missing(_))));
        assert!(!blobs.has(&hash));
    }

    #[test]
    fn blobs_survive_reopening() {
        let dir = tempfile::tempdir().unwrap();
        let hash = FsBlobs::open(dir.path()).unwrap().put(b"kept").unwrap();
        assert_eq!(
            FsBlobs::open(dir.path()).unwrap().get(&hash).unwrap(),
            b"kept"
        );
    }

    #[test]
    fn temp_files_from_killed_writes_go_on_open() {
        let dir = tempfile::tempdir().unwrap();
        let hash = FsBlobs::open(dir.path()).unwrap().put(b"kept").unwrap();
        let folder = dir.path().join("blobs").join(&hash.to_string()[..2]);
        fs::write(folder.join(".x.123.0.tmp"), b"half").unwrap();
        let blobs = FsBlobs::open(dir.path()).unwrap();
        assert_eq!(fs::read_dir(&folder).unwrap().count(), 1);
        assert_eq!(blobs.get(&hash).unwrap(), b"kept");
    }

    #[test]
    fn no_temp_files_are_left_behind() {
        let dir = tempfile::tempdir().unwrap();
        let blobs = FsBlobs::open(dir.path()).unwrap();
        for i in 0..20u8 {
            blobs.put(&[i; 100]).unwrap();
        }
        let leftovers = fs::read_dir(dir.path().join("blobs"))
            .unwrap()
            .flat_map(|d| fs::read_dir(d.unwrap().path()).unwrap())
            .filter(|f| {
                f.as_ref()
                    .unwrap()
                    .file_name()
                    .to_string_lossy()
                    .ends_with(".tmp")
            })
            .count();
        assert_eq!(leftovers, 0);
    }
}
