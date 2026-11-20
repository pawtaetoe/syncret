// everything that can go wrong in the core types
#[derive(Debug, Clone, thiserror::Error, PartialEq, Eq)]
pub enum Error {
    #[error("no randomness for a new key")]
    Random,
    #[error("not a valid public key")]
    BadKey,
    #[error("bad signature")]
    BadSignature,
    #[error("not signed by its author")]
    Unsigned,
    #[error("signed twice by the same node")]
    DuplicateSigner,
    #[error("version encoding: {0}")]
    Encoding(&'static str),
    #[error("recipe: {0}")]
    Recipe(String),
    #[error("io: {0}")]
    Io(String),
}
