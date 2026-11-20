use std::collections::HashSet;
use std::path::Path;
use std::time::Duration;

use rusqlite::{Connection, OptionalExtension, params};
use syncret_core::{Hash, Timestamp, Version};

use crate::Error;

const SCHEMA: &str = "
create table if not exists versions (
    id blob primary key,
    item text not null,
    wall_ms integer not null,
    counter integer not null,
    raw blob not null
);
create index if not exists versions_item on versions (item);
create table if not exists parents (
    child blob not null,
    parent blob not null,
    primary key (child, parent)
);
create index if not exists parents_parent on parents (parent);
create table if not exists local (
    item text primary key,
    version blob not null
);
";

// signed versions and their parent links, append only
pub struct Meta {
    pub(crate) db: Connection,
}

impl Meta {
    pub fn open(data: &Path) -> Result<Self, Error> {
        std::fs::create_dir_all(data)?;
        let db = Connection::open(data.join("meta.db"))?;
        db.busy_timeout(Duration::from_secs(5))?;
        db.pragma_update(None, "journal_mode", "wal")?;
        db.pragma_update(None, "synchronous", "full")?;
        db.execute_batch(SCHEMA)?;
        db.execute_batch(crate::journal::SCHEMA)?;
        Ok(Self { db })
    }

    // store a verified version whose parents are known and of the same item, true when new
    pub fn insert(&mut self, version: &Version) -> Result<bool, Error> {
        version.verify()?;
        if version.hlc.wall_ms > i64::MAX as u64 {
            return Err(Error::Refused("clock reading out of range".into()));
        }
        let id = version.id();
        let tx = self.db.transaction()?;
        for parent in &version.parents {
            let item: Option<String> = tx
                .query_row(
                    "select item from versions where id = ?",
                    [&parent.0[..]],
                    |r| r.get(0),
                )
                .optional()?;
            match item {
                Some(item) if item == version.item => {}
                Some(_) => return Err(Error::Refused(format!("parent {parent} is another item"))),
                None => return Err(Error::Refused(format!("parent {parent} is unknown"))),
            }
        }
        let added = tx.execute(
            "insert or ignore into versions (id, item, wall_ms, counter, raw) values (?, ?, ?, ?, ?)",
            params![
                &id.0[..],
                version.item,
                version.hlc.wall_ms as i64,
                version.hlc.counter,
                version.encode()
            ],
        )?;
        for parent in &version.parents {
            tx.execute(
                "insert or ignore into parents (child, parent) values (?, ?)",
                [&id.0[..], &parent.0[..]],
            )?;
        }
        tx.commit()?;
        Ok(added == 1)
    }

    pub fn get(&self, id: &Hash) -> Result<Option<Version>, Error> {
        self.db
            .query_row("select raw from versions where id = ?", [&id.0[..]], |r| {
                r.get::<_, Vec<u8>>(0)
            })
            .optional()?
            .map(|raw| decode(&raw))
            .transpose()
    }

    pub fn items(&self) -> Result<Vec<String>, Error> {
        let mut query = self
            .db
            .prepare("select distinct item from versions order by item")?;
        let items = query.query_map([], |r| r.get(0))?;
        Ok(items.collect::<Result<_, _>>()?)
    }

    // versions of the item that no other version names as a parent, newest first
    pub fn heads(&self, item: &str) -> Result<Vec<Version>, Error> {
        let mut query = self.db.prepare(
            "select raw from versions v where item = ?
             and not exists (select 1 from parents p where p.parent = v.id)",
        )?;
        let rows = query.query_map([item], |r| r.get::<_, Vec<u8>>(0))?;
        let mut heads = rows
            .map(|raw| decode(&raw?))
            .collect::<Result<Vec<_>, _>>()?;
        heads.sort_by_key(|v| std::cmp::Reverse(v.hlc));
        Ok(heads)
    }

    // every version reachable from id through its parents, id included
    pub fn ancestors(&self, id: &Hash) -> Result<HashSet<Hash>, Error> {
        let mut query = self.db.prepare(
            "with recursive up(id) as (
                 select ?
                 union select p.parent from parents p join up on p.child = up.id
             ) select id from up",
        )?;
        let rows = query.query_map([&id.0[..]], |r| r.get::<_, Vec<u8>>(0))?;
        rows.map(|raw| {
            let raw = raw?;
            let bytes: [u8; 32] = raw
                .try_into()
                .map_err(|_| Error::Refused("bad id in database".into()))?;
            Ok(Hash(bytes))
        })
        .collect()
    }

    // the version the disk of this node holds for item, set by every commit and apply
    pub fn local(&self, item: &str) -> Result<Option<Version>, Error> {
        let id: Option<Vec<u8>> = self
            .db
            .query_row("select version from local where item = ?", [item], |r| {
                r.get(0)
            })
            .optional()?;
        match id {
            Some(id) => {
                let id: [u8; 32] = id
                    .try_into()
                    .map_err(|_| Error::Refused("bad id in database".into()))?;
                self.get(&Hash(id))
            }
            None => Ok(None),
        }
    }

    pub fn set_local(&mut self, version: &Version) -> Result<(), Error> {
        self.db.execute(
            "insert into local (item, version) values (?, ?)
             on conflict (item) do update set version = excluded.version",
            params![version.item, &version.id().0[..]],
        )?;
        Ok(())
    }

    // the newest clock reading stored, so a restarted clock never goes below it
    pub fn newest_clock(&self) -> Result<Option<Timestamp>, Error> {
        let raw: Option<Vec<u8>> = self
            .db
            .query_row(
                "select raw from versions order by wall_ms desc, counter desc limit 1",
                [],
                |r| r.get(0),
            )
            .optional()?;
        Ok(raw.map(|raw| decode(&raw)).transpose()?.map(|v| v.hlc))
    }
}

fn decode(raw: &[u8]) -> Result<Version, Error> {
    Ok(Version::decode(raw)?)
}

#[cfg(test)]
mod tests {
    use syncret_core::NodeKey;

    use super::*;

    struct Fixture {
        _dir: tempfile::TempDir,
        meta: Meta,
        key: NodeKey,
        tick: u64,
    }

    impl Fixture {
        fn new() -> Self {
            let dir = tempfile::tempdir().unwrap();
            let meta = Meta::open(dir.path()).unwrap();
            Self {
                _dir: dir,
                meta,
                key: NodeKey::from_bytes(&[1; 32]),
                tick: 0,
            }
        }

        fn version(&mut self, item: &str, parents: &[&Version]) -> Version {
            self.tick += 1;
            let hlc = Timestamp {
                wall_ms: self.tick,
                counter: 0,
                node: self.key.id(),
            };
            let parents = parents.iter().map(|p| p.id()).collect();
            Version::new(
                &self.key,
                item,
                parents,
                Hash::of(&self.tick.to_le_bytes()),
                hlc,
                vec![],
            )
        }

        fn add(&mut self, item: &str, parents: &[&Version]) -> Version {
            let version = self.version(item, parents);
            assert!(self.meta.insert(&version).unwrap());
            version
        }

        fn head_ids(&self, item: &str) -> Vec<Hash> {
            self.meta
                .heads(item)
                .unwrap()
                .iter()
                .map(|v| v.id())
                .collect()
        }
    }

    use proptest::prelude::*;

    proptest! {
        // any order that inserts parents first ends with the same heads
        #[test]
        fn heads_do_not_depend_on_insertion_order(
            links in proptest::collection::vec(any::<u16>(), 1..14),
            priority in proptest::collection::vec(any::<u32>(), 14),
        ) {
            let mut f = Fixture::new();
            let mut all: Vec<Version> = Vec::new();
            for (i, bits) in links.iter().enumerate() {
                let parents: Vec<&Version> = (0..i).filter(|p| bits & (1 << (p % 16)) != 0).map(|p| &all[p]).collect();
                let version = f.version("file:x", &parents);
                all.push(version);
            }
            for version in &all {
                f.meta.insert(version).unwrap();
            }
            let in_order: HashSet<Hash> = f.meta.heads("file:x").unwrap().iter().map(Version::id).collect();
            let other = tempfile::tempdir().unwrap();
            let mut shuffled = Meta::open(other.path()).unwrap();
            let mut done: HashSet<Hash> = HashSet::new();
            while done.len() < all.len() {
                let next = all
                    .iter()
                    .enumerate()
                    .filter(|(_, v)| !done.contains(&v.id()) && v.parents.iter().all(|p| done.contains(p)))
                    .max_by_key(|(i, _)| priority[*i])
                    .map(|(_, v)| v.clone())
                    .unwrap();
                shuffled.insert(&next).unwrap();
                done.insert(next.id());
            }
            let other_order: HashSet<Hash> = shuffled.heads("file:x").unwrap().iter().map(Version::id).collect();
            prop_assert_eq!(in_order, other_order);
        }
    }

    #[test]
    fn a_line_has_one_head() {
        let mut f = Fixture::new();
        let a = f.add("file:x", &[]);
        let b = f.add("file:x", &[&a]);
        let c = f.add("file:x", &[&b]);
        assert_eq!(f.head_ids("file:x"), vec![c.id()]);
    }

    #[test]
    fn a_fork_has_two_heads_and_a_merge_has_one() {
        let mut f = Fixture::new();
        let a = f.add("file:x", &[]);
        let b = f.add("file:x", &[&a]);
        let c = f.add("file:x", &[&a]);
        assert_eq!(f.head_ids("file:x"), vec![c.id(), b.id()]);
        let m = f.add("file:x", &[&b, &c]);
        assert_eq!(f.head_ids("file:x"), vec![m.id()]);
        let up = f.meta.ancestors(&m.id()).unwrap();
        assert_eq!(up, HashSet::from([m.id(), b.id(), c.id(), a.id()]));
    }

    #[test]
    fn items_are_kept_apart() {
        let mut f = Fixture::new();
        let a = f.add("file:x", &[]);
        let b = f.add("file:y", &[]);
        assert_eq!(f.meta.items().unwrap(), vec!["file:x", "file:y"]);
        assert_eq!(f.head_ids("file:x"), vec![a.id()]);
        assert_eq!(f.head_ids("file:y"), vec![b.id()]);
    }

    #[test]
    fn a_bad_signature_is_refused() {
        let mut f = Fixture::new();
        let mut version = f.version("file:x", &[]);
        version.signatures[0].bytes[0] ^= 1;
        assert!(f.meta.insert(&version).is_err());
        assert!(f.meta.items().unwrap().is_empty());
    }

    #[test]
    fn unknown_or_foreign_parents_are_refused() {
        let mut f = Fixture::new();
        let orphan_parent = f.version("file:x", &[]);
        let child = f.version("file:x", &[&orphan_parent]);
        assert!(matches!(f.meta.insert(&child), Err(Error::Refused(_))));
        let other = f.add("file:y", &[]);
        let crossed = f.version("file:x", &[&other]);
        assert!(matches!(f.meta.insert(&crossed), Err(Error::Refused(_))));
    }

    #[test]
    fn inserting_twice_is_a_no_op() {
        let mut f = Fixture::new();
        let a = f.add("file:x", &[]);
        assert!(!f.meta.insert(&a).unwrap());
        assert_eq!(f.head_ids("file:x"), vec![a.id()]);
    }

    #[test]
    fn data_survives_reopening() {
        let mut f = Fixture::new();
        let a = f.add("file:x", &[]);
        let b = f.add("file:x", &[&a]);
        let reopened = Meta::open(f._dir.path()).unwrap();
        assert_eq!(reopened.get(&b.id()).unwrap(), Some(b.clone()));
        assert_eq!(reopened.heads("file:x").unwrap(), vec![b.clone()]);
        assert_eq!(reopened.newest_clock().unwrap(), Some(b.hlc));
    }
}
