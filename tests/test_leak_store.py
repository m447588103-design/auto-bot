from __future__ import annotations

from core.db import Database
from features.leak_monitor.models import LeakHit, Severity, TargetKind
from features.leak_monitor.store import LeakStore


async def make_store() -> tuple[Database, LeakStore]:
    db = Database(":memory:")
    await db.connect()
    store = LeakStore(db)
    await store.ensure_schema()
    return db, store


def hit_for(target, source: str = "hibp", external: str = "x1") -> LeakHit:
    return LeakHit(
        source=source,
        external_id=external,
        title="Test breach",
        url="https://example.com/breach",
        snippet="snippet",
        severity=Severity.HIGH,
        published_at=None,
        target=target,
        matched_value=target.value,
        fingerprint=f"{source}:{external}:{target.id}",
    )


async def test_add_and_list_targets():
    db, store = await make_store()
    try:
        target, created = await store.add_target(1, "acme.com", TargetKind.DOMAIN, label="Acme")
        assert created is True
        same, created_again = await store.add_target(1, "acme.com", TargetKind.DOMAIN)
        assert created_again is False
        assert same.id == target.id
        targets = await store.list_targets(1)
        assert len(targets) == 1
        assert targets[0].label == "Acme"
        assert await store.count_targets(1) == 1
        assert await store.count_targets(2) == 0
    finally:
        await db.close()


async def test_remove_target_scoped_to_guild():
    db, store = await make_store()
    try:
        target, _ = await store.add_target(1, "acme.com", TargetKind.DOMAIN)
        assert await store.remove_target(2, target.id) is False
        assert await store.remove_target(1, target.id) is True
        assert await store.list_targets(1) == []
    finally:
        await db.close()


async def test_hits_are_deduplicated():
    db, store = await make_store()
    try:
        target, _ = await store.add_target(1, "acme.com", TargetKind.DOMAIN)
        first = hit_for(target)
        assert await store.record_hit(first) is True
        assert await store.record_hit(first) is False
        second = hit_for(target, external="x2")
        assert await store.record_hit(second) is True
        assert await store.count_hits(1) == 2
        recent = await store.recent_hits(1, 10)
        assert len(recent) == 2
        assert recent[0]["target_value"] == "acme.com"
        assert await store.recent_hits(2) == []
    finally:
        await db.close()


async def test_channel_and_source_state():
    db, store = await make_store()
    try:
        assert await store.get_channel(1) is None
        await store.set_channel(1, 99)
        assert await store.get_channel(1) == 99
        await store.set_channel(1, 100)
        assert await store.get_channel(1) == 100

        await store.save_source_state("hibp", cursor="next", last_error=None, new_hits=2)
        state = await store.get_source_state("hibp")
        assert state.cursor == "next"
        assert state.hits_total == 2
        await store.save_source_state("hibp", cursor=None, last_error="boom", new_hits=1)
        state = await store.get_source_state("hibp")
        assert state.cursor == "next"
        assert state.last_error == "boom"
        assert state.hits_total == 3
    finally:
        await db.close()


def test_severity_rules():
    assert LeakStore.severity_for("hudsonrock", TargetKind.EMAIL) is Severity.CRITICAL
    assert LeakStore.severity_for("hibp", TargetKind.DOMAIN) is Severity.HIGH
    assert LeakStore.severity_for("psbdmp", TargetKind.EMAIL) is Severity.HIGH
    assert LeakStore.severity_for("psbdmp", TargetKind.KEYWORD) is Severity.LOW
    assert LeakStore.severity_for("unknown", TargetKind.KEYWORD) is Severity.MEDIUM
