import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field

from syncret.server.queue import Job, Queue
from syncret.server.repo import Repo


# what a job handler gets: the job, the queue to log to, and the repo it alone may write
@dataclass
class Context:
    job: Job
    queue: Queue
    repo: Repo

    def log(self, line):
        self.queue.log(self.job.id, line)


# the only writer: runs queued jobs one at a time while holding the repo lock
@dataclass
class Worker:
    queue: Queue
    repo: Repo
    handlers: dict[str, Callable[[Context], dict | None]] = field(default_factory=dict)
    poll: float = 1.0

    # run one job if one is queued; True when a job ran
    def run_once(self):
        job = self.queue.claim()
        if job is None:
            return False
        handler = self.handlers.get(job.kind)
        try:
            if handler is None:
                raise ValueError(f"no handler for job kind {job.kind!r}")
            self.queue.finish(job.id, handler(Context(job, self.queue, self.repo)))
        # any job failure is recorded on the job, never fatal to the worker
        except Exception as error:
            self.queue.fail(job.id, f"{type(error).__name__}: {error}")
        return True

    # take the lock for good, recover interrupted jobs, then run jobs as they arrive
    async def run(self):
        with self.repo.locked():
            self.repo.init()
            self.queue.recover()
            while True:
                ran = await asyncio.to_thread(self.run_once)
                if not ran:
                    await asyncio.sleep(self.poll)
