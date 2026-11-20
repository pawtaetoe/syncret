use std::fmt;

use crate::key::hex;
use crate::{Error, NodeId, NodeKey, Timestamp};

// bumped whenever the encoding changes, so old and new ids never collide
const FORMAT: u8 = 1;

// a blake3 hash naming a version or a blob
#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Hash(pub [u8; 32]);

impl Hash {
    pub fn of(bytes: &[u8]) -> Self {
        Self(*blake3::hash(bytes).as_bytes())
    }
}

impl fmt::Display for Hash {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&hex(&self.0))
    }
}

impl fmt::Debug for Hash {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "Hash({})", hex(&self.0[..6]))
    }
}

// one node signature over the body of a version
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Signature {
    pub signer: NodeId,
    pub bytes: [u8; 64],
}

// one version of an item, signed by its author and maybe approved by admins
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Version {
    pub item: String,
    pub parents: Vec<Hash>,
    pub content: Hash,
    pub author: NodeId,
    pub hlc: Timestamp,
    pub meta: Vec<u8>,
    pub signatures: Vec<Signature>,
}

impl Version {
    // a version signed by key, parents sorted so the same set always encodes the same
    pub fn new(
        key: &NodeKey,
        item: &str,
        mut parents: Vec<Hash>,
        content: Hash,
        hlc: Timestamp,
        meta: Vec<u8>,
    ) -> Self {
        parents.sort();
        parents.dedup();
        let mut version = Self {
            item: item.to_owned(),
            parents,
            content,
            author: key.id(),
            hlc,
            meta,
            signatures: Vec::new(),
        };
        version.signatures.push(Signature {
            signer: version.author,
            bytes: key.sign(&version.body()),
        });
        version
    }

    // add a signature from another node over the same body, as an approval
    pub fn approve(&mut self, key: &NodeKey) -> Result<(), Error> {
        if self.signatures.iter().any(|s| s.signer == key.id()) {
            return Err(Error::DuplicateSigner);
        }
        self.signatures.push(Signature {
            signer: key.id(),
            bytes: key.sign(&self.body()),
        });
        Ok(())
    }

    // the blake3 hash of the whole signed version
    pub fn id(&self) -> Hash {
        Hash::of(&self.encode())
    }

    // the signers other than the author, whose signatures count as approvals
    pub fn approvers(&self) -> impl Iterator<Item = NodeId> + '_ {
        self.signatures.iter().skip(1).map(|s| s.signer)
    }

    // true when the author signed first, every signature holds and the encoding is canonical
    pub fn verify(&self) -> Result<(), Error> {
        if self.hlc.node != self.author {
            return Err(Error::Encoding("clock node is not the author"));
        }
        if self.parents.windows(2).any(|w| w[0] >= w[1]) {
            return Err(Error::Encoding("parents not sorted and unique"));
        }
        match self.signatures.first() {
            Some(first) if first.signer == self.author => {}
            _ => return Err(Error::Unsigned),
        }
        let body = self.body();
        for (i, signature) in self.signatures.iter().enumerate() {
            if self.signatures[..i]
                .iter()
                .any(|s| s.signer == signature.signer)
            {
                return Err(Error::DuplicateSigner);
            }
            signature.signer.verify(&body, &signature.bytes)?;
        }
        Ok(())
    }

    // the bytes every signature covers, each field length prefixed in a fixed order
    pub fn body(&self) -> Vec<u8> {
        let mut out = vec![FORMAT];
        put_bytes(&mut out, self.item.as_bytes());
        put_u32(&mut out, self.parents.len());
        for parent in &self.parents {
            out.extend_from_slice(&parent.0);
        }
        out.extend_from_slice(&self.content.0);
        out.extend_from_slice(&self.author.0);
        out.extend_from_slice(&self.hlc.wall_ms.to_le_bytes());
        out.extend_from_slice(&self.hlc.counter.to_le_bytes());
        out.extend_from_slice(&self.hlc.node.0);
        put_bytes(&mut out, &self.meta);
        out
    }

    // the body followed by every signature
    pub fn encode(&self) -> Vec<u8> {
        let mut out = self.body();
        put_u32(&mut out, self.signatures.len());
        for signature in &self.signatures {
            out.extend_from_slice(&signature.signer.0);
            out.extend_from_slice(&signature.bytes);
        }
        out
    }

    // the inverse of encode, refusing anything it would not have written
    pub fn decode(bytes: &[u8]) -> Result<Self, Error> {
        let mut r = Reader(bytes);
        if r.take(1)? != [FORMAT] {
            return Err(Error::Encoding("unknown format"));
        }
        let item = String::from_utf8(r.bytes()?.to_vec())
            .map_err(|_| Error::Encoding("item is not utf-8"))?;
        let parents = (0..r.u32()?)
            .map(|_| r.array().map(Hash))
            .collect::<Result<_, _>>()?;
        let content = Hash(r.array()?);
        let author = NodeId(r.array()?);
        let wall_ms = u64::from_le_bytes(r.array()?);
        let counter = u32::from_le_bytes(r.array()?);
        let hlc = Timestamp {
            wall_ms,
            counter,
            node: NodeId(r.array()?),
        };
        let meta = r.bytes()?.to_vec();
        let signatures = (0..r.u32()?)
            .map(|_| {
                Ok(Signature {
                    signer: NodeId(r.array()?),
                    bytes: r.array()?,
                })
            })
            .collect::<Result<_, Error>>()?;
        if !r.0.is_empty() {
            return Err(Error::Encoding("trailing bytes"));
        }
        Ok(Self {
            item,
            parents,
            content,
            author,
            hlc,
            meta,
            signatures,
        })
    }
}

fn put_u32(out: &mut Vec<u8>, n: usize) {
    out.extend_from_slice(&(n as u32).to_le_bytes());
}

fn put_bytes(out: &mut Vec<u8>, bytes: &[u8]) {
    put_u32(out, bytes.len());
    out.extend_from_slice(bytes);
}

// reads the encoding front to back, failing on short input
struct Reader<'a>(&'a [u8]);

impl<'a> Reader<'a> {
    fn take(&mut self, n: usize) -> Result<&'a [u8], Error> {
        if self.0.len() < n {
            return Err(Error::Encoding("too short"));
        }
        let (head, rest) = self.0.split_at(n);
        self.0 = rest;
        Ok(head)
    }

    fn array<const N: usize>(&mut self) -> Result<[u8; N], Error> {
        Ok(self.take(N)?.try_into().expect("take returns n bytes"))
    }

    fn u32(&mut self) -> Result<u32, Error> {
        Ok(u32::from_le_bytes(self.array()?))
    }

    fn bytes(&mut self) -> Result<&'a [u8], Error> {
        let n = self.u32()? as usize;
        self.take(n)
    }
}

#[cfg(test)]
mod tests {
    use proptest::prelude::*;

    use super::*;

    fn key(seed: u8) -> NodeKey {
        NodeKey::from_bytes(&[seed; 32])
    }

    fn sample(key: &NodeKey) -> Version {
        let hlc = Timestamp {
            wall_ms: 1_763_640_000_000,
            counter: 3,
            node: key.id(),
        };
        Version::new(
            key,
            "file:zed-config",
            vec![Hash::of(b"b"), Hash::of(b"a")],
            Hash::of(b"content"),
            hlc,
            b"meta".to_vec(),
        )
    }

    // pinned so any change to the encoding shows up as a failing test
    #[test]
    fn id_is_stable_across_runs() {
        assert_eq!(
            sample(&key(1)).id().to_string(),
            "ce31e67ccca539c8193196008510f2ee2a7c06d1558dc492976bdb2bcc2eb281"
        );
    }

    #[test]
    fn round_trip_keeps_bytes_and_id() {
        let version = sample(&key(1));
        let decoded = Version::decode(&version.encode()).unwrap();
        assert_eq!(decoded, version);
        assert_eq!(decoded.id(), version.id());
        assert_eq!(decoded.verify(), Ok(()));
    }

    #[test]
    fn parents_are_sorted_and_unique() {
        let k = key(1);
        let mut version = sample(&k);
        assert!(version.parents[0] < version.parents[1]);
        version.parents.reverse();
        assert!(version.verify().is_err());
    }

    #[test]
    fn changing_any_field_fails_verification() {
        let k = key(1);
        let edits: [fn(&mut Version); 8] = [
            |v| v.item.push('x'),
            |v| v.parents.truncate(1),
            |v| v.content = Hash::of(b"other"),
            |v| v.author = key(2).id(),
            |v| v.hlc.wall_ms += 1,
            |v| v.hlc.counter += 1,
            |v| v.meta.push(0),
            |v| v.signatures[0].bytes[0] ^= 1,
        ];
        for edit in edits {
            let mut version = sample(&k);
            edit(&mut version);
            assert!(version.verify().is_err(), "{version:?}");
        }
    }

    #[test]
    fn moving_bytes_between_fields_changes_the_body() {
        let k = key(1);
        let hlc = sample(&k).hlc;
        let a = Version::new(&k, "ab", vec![], Hash::of(b""), hlc, b"".to_vec());
        let b = Version::new(&k, "a", vec![], Hash::of(b""), hlc, b"b".to_vec());
        assert_ne!(a.body(), b.body());
        let mut forged = b.clone();
        forged.signatures = a.signatures.clone();
        assert_eq!(forged.verify(), Err(Error::BadSignature));
    }

    #[test]
    fn another_key_cannot_sign_for_the_author() {
        let author = key(1);
        let mut version = sample(&author);
        version.signatures[0].bytes = key(2).sign(&version.body());
        assert_eq!(version.verify(), Err(Error::BadSignature));
        version.signatures[0].signer = key(2).id();
        assert_eq!(version.verify(), Err(Error::Unsigned));
    }

    #[test]
    fn approvals_verify_and_are_not_duplicated() {
        let mut version = sample(&key(1));
        version.approve(&key(2)).unwrap();
        assert_eq!(version.verify(), Ok(()));
        assert_eq!(version.approvers().collect::<Vec<_>>(), vec![key(2).id()]);
        assert_eq!(version.approve(&key(2)), Err(Error::DuplicateSigner));
        assert_eq!(version.approve(&key(1)), Err(Error::DuplicateSigner));
    }

    #[test]
    fn decode_refuses_damaged_input() {
        let bytes = sample(&key(1)).encode();
        assert!(Version::decode(&bytes[..bytes.len() - 1]).is_err());
        let mut extra = bytes.clone();
        extra.push(0);
        assert!(Version::decode(&extra).is_err());
        let mut format = bytes.clone();
        format[0] = 9;
        assert!(Version::decode(&format).is_err());
        let mut huge = bytes.clone();
        huge[1..5].copy_from_slice(&u32::MAX.to_le_bytes());
        assert!(Version::decode(&huge).is_err());
    }

    proptest! {
        #[test]
        fn any_version_round_trips(
            seed in 1u8..=255,
            item in ".{0,40}",
            parents in proptest::collection::vec(any::<[u8; 32]>(), 0..4),
            wall_ms in any::<u64>(),
            counter in any::<u32>(),
            meta in proptest::collection::vec(any::<u8>(), 0..64),
        ) {
            let k = key(seed);
            let hlc = Timestamp { wall_ms, counter, node: k.id() };
            let parents = parents.into_iter().map(Hash).collect();
            let version = Version::new(&k, &item, parents, Hash::of(&meta), hlc, meta);
            let decoded = Version::decode(&version.encode()).unwrap();
            prop_assert_eq!(decoded.id(), version.id());
            prop_assert_eq!(decoded.verify(), Ok(()));
        }
    }
}
