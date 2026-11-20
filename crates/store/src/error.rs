use syncret_core::Hash;

// everything that can go wrong in the stores
#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error("io: {0}")]
    Io(#[from] std::io::Error),
    #[error("database: {0}")]
    Database(#[from] rusqlite::Error),
    #[error(transparent)]
    Core(#[from] syncret_core::Error),
    #[error("blob {0} is missing")]
    Missing(Hash),
    #[error("blob {0} does not match its hash")]
    Corrupt(Hash),
    #[error("refused: {0}")]
    Refused(String),
}
