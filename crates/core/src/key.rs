use std::fmt;

use ed25519_dalek::{Signer, SigningKey, VerifyingKey};

use crate::Error;

// the public key of a node, which is also its id
#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct NodeId(pub [u8; 32]);

impl NodeId {
    // ok when signature is by this node over message
    pub fn verify(&self, message: &[u8], signature: &[u8; 64]) -> Result<(), Error> {
        let key = VerifyingKey::from_bytes(&self.0).map_err(|_| Error::BadKey)?;
        let signature = ed25519_dalek::Signature::from_bytes(signature);
        key.verify_strict(message, &signature)
            .map_err(|_| Error::BadSignature)
    }
}

impl fmt::Debug for NodeId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "NodeId({})", hex(&self.0[..6]))
    }
}

// the secret ed25519 key of a node, kept as 32 raw bytes so iroh can load it later
pub struct NodeKey(SigningKey);

impl NodeKey {
    pub fn generate() -> Result<Self, Error> {
        let mut bytes = [0u8; 32];
        getrandom::fill(&mut bytes).map_err(|_| Error::Random)?;
        Ok(Self::from_bytes(&bytes))
    }

    pub fn from_bytes(bytes: &[u8; 32]) -> Self {
        Self(SigningKey::from_bytes(bytes))
    }

    pub fn to_bytes(&self) -> [u8; 32] {
        self.0.to_bytes()
    }

    pub fn id(&self) -> NodeId {
        NodeId(self.0.verifying_key().to_bytes())
    }

    pub fn sign(&self, message: &[u8]) -> [u8; 64] {
        self.0.sign(message).to_bytes()
    }
}

// lowercase hex for ids in messages and logs
pub(crate) fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}
