use syncret_core::{Error, Hash};

// bumped whenever the encoding changes
const FORMAT: u8 = 1;

// what an entry in a tree is
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EntryKind {
    File,
    Link,
}

// one file or link, with its path relative to the item using forward slashes
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Entry {
    pub path: String,
    pub kind: EntryKind,
    pub mode: u32,
    pub hash: Hash,
}

// every entry of a captured tree sorted by path, so one tree always has one hash
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Manifest {
    pub entries: Vec<Entry>,
}

impl Manifest {
    pub fn new(mut entries: Vec<Entry>) -> Self {
        entries.sort_by(|a, b| a.path.cmp(&b.path));
        Self { entries }
    }

    pub fn encode(&self) -> Vec<u8> {
        let mut out = vec![FORMAT];
        out.extend_from_slice(&(self.entries.len() as u32).to_le_bytes());
        for entry in &self.entries {
            out.push(match entry.kind {
                EntryKind::File => 0,
                EntryKind::Link => 1,
            });
            out.extend_from_slice(&entry.mode.to_le_bytes());
            out.extend_from_slice(&entry.hash.0);
            out.extend_from_slice(&(entry.path.len() as u32).to_le_bytes());
            out.extend_from_slice(entry.path.as_bytes());
        }
        out
    }

    // the inverse of encode, refusing unsorted, duplicate or unsafe paths
    pub fn decode(bytes: &[u8]) -> Result<Self, Error> {
        let bad = |why| Error::Encoding(why);
        let mut rest = bytes;
        let mut take = |n: usize| {
            if rest.len() < n {
                return Err(bad("manifest too short"));
            }
            let (head, tail) = rest.split_at(n);
            rest = tail;
            Ok(head)
        };
        if take(1)? != [FORMAT] {
            return Err(bad("unknown manifest format"));
        }
        let count = u32::from_le_bytes(take(4)?.try_into().unwrap());
        let mut entries: Vec<Entry> = Vec::new();
        for _ in 0..count {
            let kind = match take(1)?[0] {
                0 => EntryKind::File,
                1 => EntryKind::Link,
                _ => return Err(bad("unknown entry kind")),
            };
            let mode = u32::from_le_bytes(take(4)?.try_into().unwrap());
            let hash = Hash(take(32)?.try_into().unwrap());
            let len = u32::from_le_bytes(take(4)?.try_into().unwrap()) as usize;
            let path = String::from_utf8(take(len)?.to_vec())
                .map_err(|_| bad("manifest path is not utf-8"))?;
            if !safe(&path) {
                return Err(bad("manifest path leaves the item"));
            }
            if entries.last().is_some_and(|last| last.path >= path) {
                return Err(bad("manifest paths not sorted and unique"));
            }
            entries.push(Entry {
                path,
                kind,
                mode,
                hash,
            });
        }
        if !rest.is_empty() {
            return Err(bad("trailing bytes after manifest"));
        }
        Ok(Self { entries })
    }
}

// a relative path made of plain parts, or empty for the item itself
pub fn safe(path: &str) -> bool {
    path.is_empty()
        || path
            .split('/')
            .all(|part| !part.is_empty() && part != "." && part != ".." && !part.contains('\\'))
            && !path.contains(':')
}

#[cfg(test)]
mod tests {
    use super::*;

    fn entry(path: &str) -> Entry {
        Entry {
            path: path.into(),
            kind: EntryKind::File,
            mode: 0o644,
            hash: Hash::of(path.as_bytes()),
        }
    }

    #[test]
    fn entries_sort_and_round_trip() {
        let manifest = Manifest::new(vec![entry("b"), entry("a/c"), entry("a")]);
        let paths: Vec<_> = manifest.entries.iter().map(|e| e.path.as_str()).collect();
        assert_eq!(paths, ["a", "a/c", "b"]);
        assert_eq!(Manifest::decode(&manifest.encode()), Ok(manifest));
    }

    #[test]
    fn unsafe_paths_are_refused() {
        for path in [
            "../x",
            "a/../b",
            "/etc/passwd",
            "a//b",
            "./a",
            "C:/x",
            "a\\..\\b",
        ] {
            let encoded = Manifest::new(vec![entry(path)]).encode();
            assert!(Manifest::decode(&encoded).is_err(), "{path}");
        }
    }

    #[test]
    fn unsorted_or_damaged_input_is_refused() {
        let mut manifest = Manifest::new(vec![entry("a"), entry("b")]);
        manifest.entries.reverse();
        assert!(Manifest::decode(&manifest.encode()).is_err());
        let bytes = Manifest::new(vec![entry("a")]).encode();
        assert!(Manifest::decode(&bytes[..bytes.len() - 1]).is_err());
    }
}
