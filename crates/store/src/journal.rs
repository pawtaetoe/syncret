use rusqlite::params;
use syncret_core::Hash;

use crate::{Error, Meta};

pub(crate) const SCHEMA: &str = "
create table if not exists applies (
    id integer primary key,
    item text not null,
    done integer not null default 0
);
create table if not exists undo (
    apply integer not null,
    step integer not null,
    path text not null,
    kind integer not null,
    hash blob,
    mode integer,
    primary key (apply, step)
);
";

// what a path held before an apply touched it
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Before {
    Missing,
    File { hash: Hash, mode: u32 },
    Link { hash: Hash },
    Folder,
}

// one recorded step of an unfinished apply
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Undo {
    pub path: String,
    pub before: Before,
}

impl Meta {
    // start a journaled apply of item and return its id
    pub fn begin_apply(&mut self, item: &str) -> Result<i64, Error> {
        self.db
            .execute("insert into applies (item) values (?)", [item])?;
        Ok(self.db.last_insert_rowid())
    }

    // record what path held before the apply changes it, committed before the change
    pub fn record_undo(&mut self, apply: i64, path: &str, before: &Before) -> Result<(), Error> {
        let (kind, hash, mode) = match before {
            Before::Missing => (0, None, None),
            Before::File { hash, mode } => (1, Some(hash.0.to_vec()), Some(*mode)),
            Before::Link { hash } => (2, Some(hash.0.to_vec()), None),
            Before::Folder => (3, None, None),
        };
        self.db.execute(
            "insert into undo (apply, step, path, kind, hash, mode)
             values (?1, (select count(*) from undo where apply = ?1), ?2, ?3, ?4, ?5)",
            params![apply, path, kind, hash, mode],
        )?;
        Ok(())
    }

    // mark an apply done and forget its undo steps
    pub fn finish_apply(&mut self, apply: i64) -> Result<(), Error> {
        let tx = self.db.transaction()?;
        tx.execute("delete from undo where apply = ?", [apply])?;
        tx.execute("update applies set done = 1 where id = ?", [apply])?;
        tx.commit()?;
        Ok(())
    }

    // every apply not marked done, each with its undo steps newest first
    pub fn unfinished_applies(&self) -> Result<Vec<(i64, Vec<Undo>)>, Error> {
        let mut applies = self
            .db
            .prepare("select id from applies where done = 0 order by id desc")?;
        let ids = applies
            .query_map([], |r| r.get::<_, i64>(0))?
            .collect::<Result<Vec<_>, _>>()?;
        let mut steps = self.db.prepare(
            "select path, kind, hash, mode from undo where apply = ? order by step desc",
        )?;
        let mut out = Vec::new();
        for id in ids {
            let rows = steps.query_map([id], |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, i64>(1)?,
                    r.get::<_, Option<Vec<u8>>>(2)?,
                    r.get::<_, Option<u32>>(3)?,
                ))
            })?;
            let mut undo = Vec::new();
            for row in rows {
                let (path, kind, hash, mode) = row?;
                let hash = || -> Result<Hash, Error> {
                    let bytes = hash
                        .clone()
                        .ok_or(Error::Refused("undo step lacks a hash".into()))?;
                    Ok(Hash(
                        bytes
                            .try_into()
                            .map_err(|_| Error::Refused("bad hash in undo".into()))?,
                    ))
                };
                let before = match kind {
                    0 => Before::Missing,
                    1 => Before::File {
                        hash: hash()?,
                        mode: mode.unwrap_or(0o644),
                    },
                    2 => Before::Link { hash: hash()? },
                    _ => Before::Folder,
                };
                undo.push(Undo { path, before });
            }
            out.push((id, undo));
        }
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unfinished_applies_come_back_newest_step_first() {
        let dir = tempfile::tempdir().unwrap();
        let mut meta = Meta::open(dir.path()).unwrap();
        let done = meta.begin_apply("file:a").unwrap();
        meta.record_undo(done, "/a", &Before::Missing).unwrap();
        meta.finish_apply(done).unwrap();
        let open = meta.begin_apply("file:b").unwrap();
        let file = Before::File {
            hash: Hash::of(b"old"),
            mode: 0o600,
        };
        meta.record_undo(open, "/b/1", &file).unwrap();
        meta.record_undo(open, "/b", &Before::Folder).unwrap();
        let reopened = Meta::open(dir.path()).unwrap();
        let unfinished = reopened.unfinished_applies().unwrap();
        assert_eq!(unfinished.len(), 1);
        assert_eq!(unfinished[0].0, open);
        let paths: Vec<_> = unfinished[0].1.iter().map(|u| u.path.as_str()).collect();
        assert_eq!(paths, ["/b", "/b/1"]);
        assert_eq!(unfinished[0].1[1].before, file);
    }
}
