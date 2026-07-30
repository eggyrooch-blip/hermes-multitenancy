"""expert_usage — per-run counter, all channels (sunke 2026-07-30 口径)."""

import sqlite3
import threading

from hermes_multitenancy import expert_usage as eu


def test_bump_creates_then_increments(tmp_path):
    db = tmp_path / "mt.db"
    assert eu.bump("kep-trevi-resource-delivery-expert", db) is True
    assert eu.bump("kep-trevi-resource-delivery-expert", db) is True
    assert eu.counts(db) == {"kep-trevi-resource-delivery-expert": 2}


def test_bump_rejects_malformed_ids(tmp_path):
    db = tmp_path / "mt.db"
    for bad in ("", "  ", "a b", "x/../y", "-leading-dash", "x" * 200, "汉字id"):
        assert eu.bump(bad, db) is False
    assert eu.counts(db) == {}


def test_bump_never_raises_on_broken_db(tmp_path):
    # a directory at the db path makes sqlite fail — bump must swallow it
    db = tmp_path / "mt.db"
    db.mkdir()
    assert eu.bump("kep-server-expert", db) is False
    assert eu.counts(db) == {}


def test_concurrent_bumps_all_land(tmp_path):
    db = tmp_path / "mt.db"
    n_threads, per_thread = 8, 5

    def worker():
        for _ in range(per_thread):
            assert eu.bump("kep-server-expert", db)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert eu.counts(db) == {"kep-server-expert": n_threads * per_thread}


def test_counts_ignores_foreign_tables(tmp_path):
    db = tmp_path / "mt.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE other (x TEXT)")
    conn.commit()
    conn.close()
    assert eu.counts(db) == {}
    assert eu.bump("kep-server-expert", db) is True
    assert eu.counts(db) == {"kep-server-expert": 1}
