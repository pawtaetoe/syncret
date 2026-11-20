use std::path::Path;

use async_trait::async_trait;

use crate::{Error, Hash, Item};

// what a kind may do on a node, the engine journals every write and remove
pub trait Ctx: Send + Sync {
    fn item(&self) -> &Item;
    fn put_blob(&self, bytes: &[u8]) -> Result<Hash, Error>;
    fn get_blob(&self, hash: &Hash) -> Result<Vec<u8>, Error>;
    fn write(&self, path: &Path, bytes: &[u8], mode: Option<u32>) -> Result<(), Error>;
    fn remove(&self, path: &Path) -> Result<(), Error>;
    fn link(&self, path: &Path, target: &Path) -> Result<(), Error>;
}

// the state of an item as content in the blob store plus kind specific metadata
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Snapshot {
    pub content: Hash,
    pub meta: Vec<u8>,
}

// a merge result has the same shape as a snapshot
pub type Merged = Snapshot;

// one kind of synced thing, merge and validate take ctx because both read content
#[async_trait]
pub trait Kind: Send + Sync {
    fn id(&self) -> &'static str;
    // whether new versions need admin approval before apply
    fn sensitive(&self) -> bool;
    // read local state into a snapshot
    async fn capture(&self, ctx: &dyn Ctx) -> Result<Snapshot, Error>;
    // combine local and remote versions
    async fn merge(
        &self,
        ctx: &dyn Ctx,
        base: Option<&Snapshot>,
        ours: &Snapshot,
        theirs: &Snapshot,
    ) -> Result<Merged, Error>;
    // reject unsafe or invalid results
    fn validate(&self, ctx: &dyn Ctx, merged: &Merged) -> Result<(), Error>;
    // write to disk and run hooks
    async fn apply(&self, ctx: &dyn Ctx, merged: &Merged) -> Result<(), Error>;
}
