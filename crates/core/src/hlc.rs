use std::time::{SystemTime, UNIX_EPOCH};

use crate::NodeId;

// a hybrid logical clock reading, ordered by wall time then counter then node
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Timestamp {
    pub wall_ms: u64,
    pub counter: u32,
    pub node: NodeId,
}

// where the clock reads wall time, so tests can move it
pub trait WallClock: Send + Sync {
    fn now_ms(&self) -> u64;
}

// the real wall clock
pub struct SystemClock;

impl WallClock for SystemClock {
    fn now_ms(&self) -> u64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_millis() as u64)
            .unwrap_or(0)
    }
}

// hands out strictly increasing timestamps however the wall clock moves
pub struct Hlc<C: WallClock = SystemClock> {
    node: NodeId,
    clock: C,
    wall_ms: u64,
    counter: u32,
}

impl<C: WallClock> Hlc<C> {
    // a clock that never goes below last, the newest timestamp this node already used
    pub fn new(node: NodeId, clock: C, last: Option<Timestamp>) -> Self {
        let mut hlc = Self {
            node,
            clock,
            wall_ms: 0,
            counter: 0,
        };
        if let Some(last) = last {
            hlc.observe(last);
        }
        hlc
    }

    pub fn now(&mut self) -> Timestamp {
        let wall_ms = self.clock.now_ms();
        if wall_ms > self.wall_ms {
            self.wall_ms = wall_ms;
            self.counter = 0;
        } else if self.counter == u32::MAX {
            self.wall_ms += 1;
            self.counter = 0;
        } else {
            self.counter += 1;
        }
        Timestamp {
            wall_ms: self.wall_ms,
            counter: self.counter,
            node: self.node,
        }
    }

    // move past a timestamp seen from another node, so later ones order after it
    pub fn observe(&mut self, seen: Timestamp) {
        if (seen.wall_ms, seen.counter) > (self.wall_ms, self.counter) {
            self.wall_ms = seen.wall_ms;
            self.counter = seen.counter;
        }
    }
}

#[cfg(test)]
mod tests {
    use std::sync::atomic::{AtomicU64, Ordering};

    use proptest::prelude::*;

    use super::*;

    // a wall clock the test sets by hand
    struct Manual(AtomicU64);

    impl WallClock for &Manual {
        fn now_ms(&self) -> u64 {
            self.0.load(Ordering::SeqCst)
        }
    }

    fn node(seed: u8) -> NodeId {
        NodeId([seed; 32])
    }

    #[test]
    fn increases_when_the_wall_clock_goes_back_or_stands_still() {
        let wall = Manual(AtomicU64::new(1_000));
        let mut hlc = Hlc::new(node(1), &wall, None);
        let a = hlc.now();
        let b = hlc.now();
        wall.0.store(400, Ordering::SeqCst);
        let c = hlc.now();
        wall.0.store(5_000, Ordering::SeqCst);
        let d = hlc.now();
        assert!(a < b && b < c && c < d);
        assert_eq!((c.wall_ms, c.counter), (1_000, 2));
        assert_eq!((d.wall_ms, d.counter), (5_000, 0));
    }

    #[test]
    fn orders_after_an_observed_remote_timestamp() {
        let wall = Manual(AtomicU64::new(1_000));
        let mut hlc = Hlc::new(node(1), &wall, None);
        let remote = Timestamp {
            wall_ms: 9_000,
            counter: 7,
            node: node(2),
        };
        hlc.observe(remote);
        assert!(hlc.now() > remote);
    }

    #[test]
    fn resumes_above_the_last_timestamp_used() {
        let wall = Manual(AtomicU64::new(10));
        let last = Timestamp {
            wall_ms: 50_000,
            counter: 4,
            node: node(1),
        };
        let mut hlc = Hlc::new(node(1), &wall, Some(last));
        assert!(hlc.now() > last);
    }

    #[test]
    fn ties_order_by_node() {
        let a = Timestamp {
            wall_ms: 5,
            counter: 1,
            node: node(1),
        };
        let b = Timestamp { node: node(2), ..a };
        assert!(a < b);
        assert_eq!(a.cmp(&b), a.cmp(&b));
    }

    #[test]
    fn counter_overflow_moves_the_wall_forward() {
        let wall = Manual(AtomicU64::new(100));
        let mut hlc = Hlc::new(node(1), &wall, None);
        hlc.observe(Timestamp {
            wall_ms: 100,
            counter: u32::MAX,
            node: node(2),
        });
        let next = hlc.now();
        assert_eq!((next.wall_ms, next.counter), (101, 0));
    }

    #[derive(Debug, Clone)]
    enum Step {
        Set(u64),
        Now,
        Observe(u64, u32),
    }

    fn step() -> impl Strategy<Value = Step> {
        prop_oneof![
            (0u64..10_000).prop_map(Step::Set),
            Just(Step::Now),
            (0u64..10_000, any::<u32>()).prop_map(|(w, c)| Step::Observe(w, c)),
        ]
    }

    proptest! {
        #[test]
        fn never_hands_out_a_timestamp_below_one_seen_or_used(
            steps in proptest::collection::vec(step(), 1..200)
        ) {
            let wall = Manual(AtomicU64::new(0));
            let mut hlc = Hlc::new(node(1), &wall, None);
            let mut floor: Option<Timestamp> = None;
            for step in steps {
                match step {
                    Step::Set(ms) => wall.0.store(ms, Ordering::SeqCst),
                    Step::Observe(wall_ms, counter) => {
                        let seen = Timestamp { wall_ms, counter, node: node(2) };
                        hlc.observe(seen);
                        floor = floor.max(Some(seen));
                    }
                    Step::Now => {
                        let next = hlc.now();
                        if let Some(floor) = floor {
                            prop_assert!(next > floor);
                        }
                        floor = Some(next);
                    }
                }
            }
        }
    }
}
