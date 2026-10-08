"""Cross-process compare-and-set for billing vault rows (audit MT #01).

The billing manager's ``threading.RLock`` only serializes one process. The
broker (``mark_invalid``) and the oneshot refresh service
(``adopt_employee_key``) are two processes sharing one SQLite file, so a
read-modify-write that loaded v3 could overwrite a v4 another process had
just committed. These tests pin the fix: billing writes that carry a load-time
snapshot go through ``CredentialStore.put_credential_if`` and a stale write is
refused instead of clobbering the newer row.
"""
from __future__ import annotations

import logging
import multiprocessing as mp
import os
import queue as queue_mod
import sqlite3
import time
from pathlib import Path

import pytest

import hermes_multitenancy

KEY = "save-cas-test-only-not-a-real-key-0123456789"
EMP = PROF = "zhangsan"
EMAIL = "zhangsan@example.com"
DAY_MS = 86_400_000
_WORKTREE = Path(__file__).resolve().parents[1]


def test_imports_resolve_to_this_worktree():
    resolved = Path(hermes_multitenancy.__file__).resolve()
    print("hermes_multitenancy.__file__ =", resolved)
    assert resolved.is_relative_to(_WORKTREE), resolved


# ---------------------------------------------------------------- helpers


def _manager(db: str):
    from hermes_multitenancy.billing_credentials import BillingCredentialManager
    from hermes_multitenancy.credentials import CredentialStore

    class _NoGateway:
        def ensure(self, **kw):  # pragma: no cover - must never be reached
            raise AssertionError("gateway must not be called")

    return BillingCredentialManager(
        vault=CredentialStore(db, encryption_key=KEY),
        gateway=_NoGateway(),
        model_base_url="https://invalid.example",
        probe=lambda key: None,
    )


def _issued(version_tag: str, *, days: int = 30):
    from hermes_multitenancy.billing_employee_key import IssuedKey

    return IssuedKey(
        EMP, EMAIL, f"sk-{version_tag}", "https://invalid.example",
        f"alias-{version_tag}", "team-a",
        int(time.time() * 1000) + days * DAY_MS, "lu-1", "t-1", True,
    )


def _payer():
    from hermes_multitenancy.billing_credentials import _ResolvedPayer

    return _ResolvedPayer(EMP, PROF, EMAIL, "")


def _seed(db: str, version: int, tag: str) -> dict:
    from hermes_multitenancy.billing_employee_key import to_vault_payload

    payload = to_vault_payload(
        _issued(tag, days=20), profile_name=PROF, credential_version=version
    )
    _manager(db)._save_payload(PROF, EMP, payload)
    return payload


def _meta(payload: dict) -> dict:
    return {
        "litellm_billing_employee_user_id": EMP,
        "litellm_billing_profile_name": PROF,
        "litellm_billing_user_id": payload["litellm_user_id"],
        "litellm_billing_team_id": payload["team_id"],
        "litellm_billing_key_id": payload["key_id"],
        "litellm_billing_credential_version": payload["credential_version"],
    }


def _row(db: str) -> dict:
    return _manager(db)._load_payload(PROF, EMP)


# --------------------------------------------- two-process race (MT #01)


def _proc_a_mark_invalid(db, meta, loaded_evt, b_done_evt, out):
    """Broker side: mark_invalid, paused after its load until B committed."""
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Capture(level=logging.WARNING)
    logging.getLogger("hermes_multitenancy").addHandler(handler)
    try:
        m = _manager(db)
        orig = m._load_payload
        calls = {"n": 0}

        def slow_first_load(*a, **k):
            r = orig(*a, **k)
            calls["n"] += 1
            if calls["n"] == 1:
                loaded_evt.set()
                b_done_evt.wait(30)
            return r

        m._load_payload = slow_first_load
        m.mark_invalid(meta)
        warnings = [r.getMessage() for r in records if r.levelno >= logging.WARNING]
        out.put(("A", "ok", warnings))
    except BaseException as exc:  # surface any raise to the parent
        out.put(("A", f"raised {type(exc).__name__}: {exc}", []))


def _proc_b_adopt(db, loaded_evt, b_done_evt, out):
    """Refresh side: real adopt_employee_key writes v4 inside A's window."""
    try:
        if not loaded_evt.wait(30):
            out.put(("B", "timeout waiting for A", None))
            return
        binding = _manager(db).adopt_employee_key(_payer(), _issued("v4"))
        out.put(("B", "ok", binding.credential_version))
    except BaseException as exc:
        out.put(("B", f"raised {type(exc).__name__}: {exc}", None))
    finally:
        b_done_evt.set()


def test_stale_mark_invalid_from_another_process_does_not_overwrite_v4(tmp_path):
    db = str(tmp_path / "multitenancy.db")
    v3 = _seed(db, 3, "v3")

    ctx = mp.get_context("spawn")
    loaded, b_done, out = ctx.Event(), ctx.Event(), ctx.Queue()
    a = ctx.Process(target=_proc_a_mark_invalid, args=(db, _meta(v3), loaded, b_done, out))
    b = ctx.Process(target=_proc_b_adopt, args=(db, loaded, b_done, out))
    a.start()
    b.start()
    b.join(30)
    a.join(30)
    for p in (a, b):
        if p.is_alive():
            p.kill()
    assert (a.exitcode, b.exitcode) == (0, 0), (a.exitcode, b.exitcode)

    results = {}
    try:
        while True:
            who, status, extra = out.get(timeout=1)
            results[who] = (status, extra)
    except queue_mod.Empty:
        pass
    assert results["B"] == ("ok", 4), results
    assert results["A"][0] == "ok", results  # stale write refused, not raised

    final = _row(db)
    verdict = (
        "LOST_UPDATE"
        if final["credential_version"] == 3 and final.get("invalid")
        else "NO_LOST_UPDATE"
    )
    print(
        f"FINAL row: key_id={final['key_id']} credential_version="
        f"{final['credential_version']} invalid={final.get('invalid')} -> {verdict}"
    )
    assert verdict == "NO_LOST_UPDATE"
    assert final["credential_version"] == 4
    assert final["key_id"] == "alias-v4"
    assert not final.get("invalid")
    assert len(results["A"][1]) == 1, results["A"][1]  # exactly one warning


def test_mark_invalid_normal_path_still_marks(tmp_path):
    db = str(tmp_path / "multitenancy.db")
    v3 = _seed(db, 3, "v3")
    _manager(db).mark_invalid(_meta(v3))
    final = _row(db)
    assert final["credential_version"] == 3
    assert final["key_id"] == "alias-v3"
    assert final["invalid"] is True


def test_mark_invalid_retries_once_when_same_key_was_rewritten(tmp_path):
    """CAS lost to a writer that kept the same key (e.g. another invalid mark
    bumped nothing but rewrote the row with a new version): reload once and
    mark on the fresh snapshot."""
    db = str(tmp_path / "multitenancy.db")
    v3 = _seed(db, 3, "v3")
    m = _manager(db)
    orig = m._load_payload
    calls = {"n": 0}

    def load_then_rewrite(*a, **k):
        r = orig(*a, **k)
        calls["n"] += 1
        if calls["n"] == 1:
            bumped = dict(r)
            bumped["credential_version"] = 7  # same key_id, new version
            _manager(db)._save_payload(PROF, EMP, bumped)
        return r

    m._load_payload = load_then_rewrite
    m.mark_invalid(_meta(v3))
    final = _row(db)
    assert calls["n"] == 2
    assert final["key_id"] == "alias-v3"
    assert final["credential_version"] == 7
    assert final["invalid"] is True


# ------------------------------------------------------ store_binding rollback


class _Preparer:
    def __init__(self, credentials, store):
        self._credentials = credentials
        self._store = store


def test_rollback_restores_previous_when_nobody_wrote(tmp_path):
    from hermes_multitenancy.billing_employee_key import store_binding

    db = str(tmp_path / "multitenancy.db")
    _seed(db, 3, "v3")

    class _Failing:
        def put(self, binding):
            assert binding.credential_version == 4
            raise RuntimeError("identity store down")

    with pytest.raises(RuntimeError):
        store_binding(_Preparer(_manager(db), _Failing()), _payer(), _issued("v4"))
    final = _row(db)
    assert final["credential_version"] == 3
    assert final["key_id"] == "alias-v3"


def test_rollback_skipped_when_another_writer_landed_v5(tmp_path, caplog):
    from hermes_multitenancy.billing_employee_key import store_binding

    db = str(tmp_path / "multitenancy.db")
    _seed(db, 3, "v3")

    class _FailingAfterRace:
        def put(self, binding):
            assert binding.credential_version == 4
            # Another process (separate store + manager) rotates to v5 first.
            other = _manager(db).adopt_employee_key(_payer(), _issued("v5"))
            assert other.credential_version == 5
            raise RuntimeError("identity store down")

    with caplog.at_level(logging.WARNING, logger="hermes_multitenancy"):
        with pytest.raises(RuntimeError):
            store_binding(
                _Preparer(_manager(db), _FailingAfterRace()), _payer(), _issued("v4")
            )
    final = _row(db)
    assert final["credential_version"] == 5
    assert final["key_id"] == "alias-v5"
    assert any("rollback" in r.getMessage() for r in caplog.records), caplog.text


def test_first_adopt_rollback_does_not_delete_concurrent_v2(tmp_path, caplog):
    """codex review p1: first-ever adopt writes v1, another process rotates to
    v2, then the identity write fails. The empty-previous rollback must not
    delete v2 (that would orphan the gateway key and degrade the employee)."""
    from hermes_multitenancy.billing_employee_key import store_binding

    db = str(tmp_path / "multitenancy.db")

    class _FailingAfterRace:
        def put(self, binding):
            assert binding.credential_version == 1
            other = _manager(db).adopt_employee_key(_payer(), _issued("v2"))
            assert other.credential_version == 2
            raise RuntimeError("identity store down")

    with caplog.at_level(logging.WARNING, logger="hermes_multitenancy"):
        with pytest.raises(RuntimeError):
            store_binding(
                _Preparer(_manager(db), _FailingAfterRace()), _payer(), _issued("v1")
            )
    final = _row(db)
    assert final is not None, "concurrent v2 was deleted by the rollback"
    assert final["credential_version"] == 2
    assert final["key_id"] == "alias-v2"
    assert any("rollback" in r.getMessage() for r in caplog.records), caplog.text


def test_first_adopt_rollback_deletes_own_v1_when_nobody_wrote(tmp_path):
    from hermes_multitenancy.billing_employee_key import store_binding

    db = str(tmp_path / "multitenancy.db")

    class _Failing:
        def put(self, binding):
            raise RuntimeError("identity store down")

    with pytest.raises(RuntimeError):
        store_binding(_Preparer(_manager(db), _Failing()), _payer(), _issued("v1"))
    assert _row(db) is None


# ------------------------------------------------------ put_credential_if unit


def _store(tmp_path):
    from hermes_multitenancy.credentials import CredentialStore

    return CredentialStore(tmp_path / "vault.db", encryption_key=KEY)


_IDS = dict(profile_name="p1", subject_id="s1", provider="litellm", secret_kind="k")


def test_put_credential_if_false_keeps_row_and_returns_false(tmp_path):
    store = _store(tmp_path)
    assert store.put_credential(**_IDS, payload={"v": 1}) is True
    seen = []
    ok = store.put_credential_if(
        **_IDS, payload={"v": 2}, expect=lambda cur: seen.append(cur) or False
    )
    assert ok is False
    assert seen == [{"v": 1}]
    assert store.get_secret_for_runtime(**_IDS) == {"v": 1}


def test_put_credential_if_true_updates_row(tmp_path):
    store = _store(tmp_path)
    store.put_credential(**_IDS, payload={"v": 1})
    ok = store.put_credential_if(
        **_IDS, payload={"v": 2}, expires_at=123, expect=lambda cur: cur == {"v": 1}
    )
    assert ok is True
    assert store.get_secret_for_runtime(**_IDS) == {"v": 2}


def test_put_credential_if_sees_none_for_missing_row_and_can_insert(tmp_path):
    store = _store(tmp_path)
    seen = []
    assert store.put_credential_if(
        **_IDS, payload={"v": 1}, expect=lambda cur: seen.append(cur) or cur is None
    ) is True
    assert seen == [None]
    assert store.get_secret_for_runtime(**_IDS) == {"v": 1}


def test_put_credential_if_reads_old_row_under_write_lock(tmp_path):
    """expect() runs inside BEGIN IMMEDIATE: another connection cannot take
    the write lock while the old row is being judged."""
    store = _store(tmp_path)
    store.put_credential(**_IDS, payload={"v": 1})
    probe = {}

    def expect(cur):
        other = sqlite3.connect(store.db_path, timeout=0)
        try:
            other.execute("BEGIN IMMEDIATE")
            probe["locked"] = False
            other.rollback()
        except sqlite3.OperationalError as exc:
            probe["locked"] = "locked" in str(exc)
        finally:
            other.close()
        return True

    assert store.put_credential_if(**_IDS, payload={"v": 2}, expect=expect) is True
    assert probe == {"locked": True}
    # Transaction closed: a fresh writer gets the lock immediately.
    other = sqlite3.connect(store.db_path, timeout=0)
    other.execute("BEGIN IMMEDIATE")
    other.rollback()
    other.close()


def test_put_credential_if_rolls_back_when_expect_raises(tmp_path):
    store = _store(tmp_path)
    store.put_credential(**_IDS, payload={"v": 1})

    def boom(cur):
        raise ValueError("nope")

    with pytest.raises(ValueError):
        store.put_credential_if(**_IDS, payload={"v": 2}, expect=boom)
    assert store._conn.in_transaction is False
    assert store.get_secret_for_runtime(**_IDS) == {"v": 1}


def test_delete_credential_if_false_keeps_row(tmp_path):
    store = _store(tmp_path)
    store.put_credential(**_IDS, payload={"v": 1})
    seen = []
    assert store.delete_credential_if(
        **_IDS, expect=lambda cur: seen.append(cur) or False
    ) is False
    assert seen == [{"v": 1}]
    assert store.get_secret_for_runtime(**_IDS) == {"v": 1}


def test_delete_credential_if_true_deletes_under_write_lock(tmp_path):
    store = _store(tmp_path)
    store.put_credential(**_IDS, payload={"v": 1})
    probe = {}

    def expect(cur):
        other = sqlite3.connect(store.db_path, timeout=0)
        try:
            other.execute("BEGIN IMMEDIATE")
            probe["locked"] = False
            other.rollback()
        except sqlite3.OperationalError as exc:
            probe["locked"] = "locked" in str(exc)
        finally:
            other.close()
        return cur == {"v": 1}

    assert store.delete_credential_if(**_IDS, expect=expect) is True
    assert probe == {"locked": True}
    assert store.get_status(**_IDS)["status"] == "missing"
    assert store._conn.in_transaction is False


def test_delete_credential_if_missing_row_passes_none(tmp_path):
    store = _store(tmp_path)
    seen = []
    assert store.delete_credential_if(
        **_IDS, expect=lambda cur: seen.append(cur) or False
    ) is False
    assert seen == [None]


def test_vault_connection_busy_timeout_is_at_least_5s(tmp_path):
    store = _store(tmp_path)
    (busy,) = store._conn.execute("PRAGMA busy_timeout").fetchone()
    assert busy >= 5000
