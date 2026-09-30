from datetime import timedelta
from pathlib import Path

import pytest

from vpipe_api.jobs.models import JobRecord, JobStatus, new_job_id, utcnow
from vpipe_api.jobs.store import JobStore, StoreLockedError


def test_ids_are_unique_and_sortable() -> None:
    ids = [new_job_id() for _ in range(200)]
    assert len(set(ids)) == 200
    assert all(i.startswith("job_") and len(i) == 30 for i in ids)


def test_save_get_roundtrip(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    record = store.save(JobRecord(workflow="w", params={"a": 1}))
    assert store.get(record.id) == record
    assert not list(store.job_dir(record.id).glob(".*.tmp"))


@pytest.mark.parametrize("bad", ["", "../x", ".hidden", "a/b", "missing"])
def test_get_unknown_or_unsafe(tmp_path: Path, bad: str) -> None:
    assert JobStore(tmp_path).get(bad) is None


def test_corrupt_record_is_ignored(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    record = store.save(JobRecord(workflow="w", params={}))
    (store.job_dir(record.id) / "job.json").write_text("{broken")
    assert store.get(record.id) is None
    assert store.all() == []


def test_transitions_are_immutable() -> None:
    record = JobRecord(workflow="w", params={})
    running = record.started()
    assert record.status is JobStatus.QUEUED and running.status is JobStatus.RUNNING
    done = running.succeeded({"x": 1}, "output.mp4")
    assert done.status.finished and done.finished_at is not None
    failed = running.failed("c", "m", retryable=True)
    assert failed.error is not None and failed.error.retryable


def test_recover_after_restart(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    queued = store.save(JobRecord(workflow="w", params={}))
    running = store.save(JobRecord(workflow="w", params={}).started())
    done = store.save(JobRecord(workflow="w", params={}).canceled())
    requeue, failed = store.recover()
    assert [r.id for r in requeue] == [queued.id]
    assert [r.id for r in failed] == [running.id]
    recovered = store.get(running.id)
    assert recovered is not None and recovered.error is not None
    assert recovered.error.code == "server_restarted"
    assert store.get(done.id) == done


def test_prune_old_finished_jobs(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    old = JobRecord(workflow="w", params={}).canceled()
    old = store.save(old.model_copy(update={"finished_at": utcnow() - timedelta(days=10)}))
    fresh = store.save(JobRecord(workflow="w", params={}).canceled())
    queued = store.save(
        JobRecord(workflow="w", params={}, created_at=utcnow() - timedelta(days=30))
    )
    assert store.prune(7) == 1
    assert store.get(old.id) is None
    assert store.get(fresh.id) is not None and store.get(queued.id) is not None


def test_instance_lock(tmp_path: Path) -> None:
    first, second = JobStore(tmp_path), JobStore(tmp_path)
    first.acquire_instance_lock()
    with pytest.raises(StoreLockedError):
        second.acquire_instance_lock()
    first.release_instance_lock()
    second.acquire_instance_lock()
    second.release_instance_lock()
