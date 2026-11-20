import asyncio
import threading
import time

import pytest

from syncret.server.queue import Queue
from syncret.server.repo import Repo, RepoLocked
from syncret.server.worker import Worker


@pytest.fixture
def queue(tmp_path):
    return Queue(tmp_path / "data" / "syncret.db")


# jobs are claimed oldest first, finished with a result, and listed newest first
def test_lifecycle(queue):
    first = queue.enqueue("vet", {"proposal": 1})
    second = queue.enqueue("scan")
    job = queue.claim()
    assert job is not None
    assert (job.id, job.kind, job.args, job.state, job.attempts) == (first, "vet",
                                                                     {"proposal": 1},
                                                                     "running", 1)
    queue.log(first, "checking rules")
    queue.finish(first, {"verdict": "pass"})
    done = queue.get(first)
    assert done is not None
    assert (done.state, done.result, done.log) == ("done", {"verdict": "pass"},
                                                   "checking rules\n")
    assert [j.id for j in queue.jobs()] == [second, first]
    assert queue.get(999) is None


# many claimers at once never get the same job
def test_claim_is_atomic(queue):
    ids = [queue.enqueue("vet") for _ in range(40)]
    claimed, lock = [], threading.Lock()

    def claimer():
        while (job := Queue(queue.path).claim()) is not None:
            with lock:
                claimed.append(job.id)

    threads = [threading.Thread(target=claimer) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(claimed) == ids


# a failing job is retried until it runs out of attempts
def test_retries(tmp_path, queue):
    calls = []

    def flaky(ctx):
        calls.append(ctx.job.attempts)
        raise RuntimeError("upstream down")

    job_id = queue.enqueue("add", max_attempts=2)
    with Repo(tmp_path / "data").locked() as repo:
        worker = Worker(queue, repo, {"add": flaky})
        assert worker.run_once()
        job = queue.get(job_id)
        assert job is not None
        assert job.state == "queued"
        assert worker.run_once()
        assert not worker.run_once()
    job = queue.get(job_id)
    assert job is not None
    assert (job.state, job.error, calls) == ("failed", "RuntimeError: upstream down", [1, 2])


# a job of an unknown kind fails instead of stopping the worker
def test_unknown_kind(tmp_path, queue):
    job_id = queue.enqueue("dance", max_attempts=1)
    with Repo(tmp_path / "data").locked() as repo:
        assert Worker(queue, repo).run_once()
    job = queue.get(job_id)
    assert job is not None
    assert job.state == "failed"
    assert "no handler for job kind 'dance'" in (job.error or "")


# a job left running by a restart is put back and retried; out of attempts, it fails
def test_recover(queue):
    retried = queue.enqueue("vet", max_attempts=3)
    spent = queue.enqueue("scan", max_attempts=1)
    queue.claim()
    queue.claim()
    restarted = Queue(queue.path)
    restarted.recover()
    first, second = restarted.get(retried), restarted.get(spent)
    assert first is not None
    assert second is not None
    assert (first.state, first.error) == ("queued", "interrupted by a restart")
    assert second.state == "failed"
    job = restarted.claim()
    assert job is not None
    assert (job.id, job.attempts) == (retried, 2)


# the log can be read while the job is still running
def test_log_while_running(tmp_path, queue):
    seen = []

    def handler(ctx):
        ctx.log("step one")
        job = queue.get(ctx.job.id)
        assert job is not None
        seen.append((job.state, job.log))

    queue.enqueue("vet")
    with Repo(tmp_path / "data").locked() as repo:
        Worker(queue, repo, {"vet": handler}).run_once()
    assert seen == [("running", "step one\n")]


# two jobs queued at once run one after the other, and a second worker cannot start
def test_worker_runs_jobs_one_at_a_time(tmp_path, queue):
    spans = []

    def slow(ctx):
        start = time.monotonic()
        time.sleep(0.2)
        spans.append((start, time.monotonic()))
        return {"ok": True}

    async def scenario():
        repo = Repo(tmp_path / "data")
        worker = Worker(queue, repo, {"vet": slow}, poll=0.05)
        task = asyncio.create_task(worker.run())
        a, b = queue.enqueue("vet"), queue.enqueue("vet")
        for _ in range(100):
            await asyncio.sleep(0.05)
            jobs = [queue.get(a), queue.get(b)]
            if all(j is not None and j.state == "done" for j in jobs):
                break
        with pytest.raises(RepoLocked):
            await Worker(queue, Repo(tmp_path / "data")).run()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert len(spans) == 2
    (_, end_a), (start_b, _) = sorted(spans)
    assert start_b >= end_a
    assert Repo(tmp_path / "data").skills() == {"skills": {}}
