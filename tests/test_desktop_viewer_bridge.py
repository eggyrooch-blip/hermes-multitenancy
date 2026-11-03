"""Bot Desktop viewer bridge on the run broker: owner-only auth, viewer_id lifecycle, core lease and
RFB filter wiring, WebSocket close codes and the idle-sweep exemption while a human drives.

Runs against the real run broker app (Bearer + owner header + routing table) with podman mocked; the
RFB side is a fake Xvnc on a real Unix socket so the WebSocket pump is exercised byte for byte."""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from hermes_multitenancy import desktop_sandbox as ds
from hermes_multitenancy import desktop_viewer as dv

KEY = "test-broker-key"
OWNER = "ou_owner"
OTHER = "ou_other"
AUTH = {"Authorization": f"Bearer {KEY}"}

RFB_VERSION = b"RFB 003.008\n"
CLIENT_HANDSHAKE = RFB_VERSION + b"\x01" + b"\x00"  # version, security type None, ClientInit(shared=0)
POINTER = bytes([5, 0, 0, 10, 0, 20])  # PointerEvent mask=0 x=10 y=20
FB_REQUEST = bytes([3, 0, 0, 0, 0, 0, 5, 160, 3, 132])  # FramebufferUpdateRequest 1440x900


def _hdr(owner: str = OWNER, **extra: str) -> dict[str, str]:
    return {**AUTH, dv.OWNER_HEADER: owner, **extra}


class FakeXvnc:
    """Records every client byte that crosses the bridge; sends the RFB version on connect."""

    def __init__(self, path: Path):
        self.path = path
        self.received = bytearray()
        self.server: asyncio.AbstractServer | None = None
        self.writers: list[asyncio.StreamWriter] = []

    async def start(self) -> None:
        async def on_conn(reader, writer):
            self.writers.append(writer)
            writer.write(RFB_VERSION)
            await writer.drain()
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                self.received += data
            writer.close()

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.server = await asyncio.start_unix_server(on_conn, path=str(self.path))

    async def hang_up(self) -> None:
        for w in self.writers:
            w.close()
        assert self.server is not None
        self.server.close()

    async def wait_for(self, n: int, timeout: float = 3.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while len(self.received) < n:
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(f"fake Xvnc saw {len(self.received)} bytes, wanted {n}")
            await asyncio.sleep(0.02)


@pytest.fixture
def world(monkeypatch):
    """Short root (AF_UNIX path limit), routing table, profiles, broker key, audit on."""
    root = Path(tempfile.mkdtemp(prefix="dv", dir="/tmp")).resolve()
    shared = root / "sh"
    profiles = shared / "profiles"
    for name in ("mine", "theirs", "plain", "agentp"):
        (profiles / name).mkdir(parents=True)
    audit = root / "security.jsonl"
    monkeypatch.setenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", KEY)
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_ENABLED", "1")
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_PATH", str(audit))
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared))

    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.routing import RoutingTable

    db = root / "routing.db"
    table = RoutingTable(db)
    table.upsert(user_id="u-owner", profile_name="mine", open_id=OWNER, provenance="sync")
    table.upsert(user_id="u-other", profile_name="theirs", open_id=OTHER, provenance="sync")
    table.upsert_owned_agent(agent_id="webui:ou_other:shared", profile_name="agentp", owner_open_id=OTHER)
    table.grant_agent_share(agent_id="webui:ou_other:shared", grantee_open_id=OWNER, role="editor",
                            created_by_open_id=OTHER)
    table.close()
    router_mod.override_routing_table(db)
    monkeypatch.setattr(router_mod, "_profile_name_to_home", lambda name: profiles / name)

    enabled = {"mine", "theirs", "agentp"}

    def decision(home: Path):
        cfg = {"multitenancy": {"desktop": {"enabled": home.name in enabled}}}
        return ds.desktop_decision({}, home, profile_config=cfg)

    monkeypatch.setattr(dv, "desktop_decision", decision)
    running = {"value": True}
    monkeypatch.setattr(dv, "_container_running", lambda d: running["value"])
    dv.VIEWERS.clear()
    yield {"root": root, "shared": shared, "profiles": profiles, "audit": audit, "running": running,
           "enabled": enabled}
    router_mod.override_routing_table(None)
    dv.VIEWERS.clear()
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
async def client(world):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    app = create_run_broker_app(mark_seen=lambda _r: True, sandbox_available=lambda: True)
    c = TestClient(TestServer(app))
    await c.start_server()
    yield c
    await c.close()


def _lease(home: Path) -> dict:
    p = home / "bot-desktop" / "lease.json"
    return json.loads(p.read_text()) if p.exists() else {"holder": "agent", "epoch": 0}


def _audit(world) -> list[dict]:
    p = world["audit"]
    return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []


async def _observe(client, owner: str = OWNER, **headers: str) -> dict:
    resp = await client.post("/api/run-broker/desktop/observe", headers=_hdr(owner, **headers), json={})
    assert resp.status == 200, await resp.text()
    return await resp.json()


# --- auth / ownership --------------------------------------------------------------------------


async def test_routes_require_bearer(client):
    for path in ("observe", "ensure", "lease/acquire", "lease/release"):
        resp = await client.post(f"/api/run-broker/desktop/{path}", headers={dv.OWNER_HEADER: OWNER}, json={})
        assert resp.status == 401, path
    resp = await client.get("/api/run-broker/desktop/ws?viewer_id=x", headers={dv.OWNER_HEADER: OWNER})
    assert resp.status == 401


async def test_owner_header_required_and_other_owners_agent_forbidden(client, world):
    resp = await client.post("/api/run-broker/desktop/observe", headers=AUTH, json={})
    assert resp.status == 403
    # Another owner's agent, named by agent id without a share: the broker resolver refuses.
    resp = await client.post("/api/run-broker/desktop/observe", headers=_hdr(OTHER), json={})
    assert resp.status == 200
    resp = await client.post("/api/run-broker/desktop/observe",
                             headers=_hdr("ou_stranger", **{"X-Hermes-Agent-Id": "webui:ou_other:shared"}),
                             json={})
    assert resp.status == 403


async def test_share_grantee_is_refused_even_when_broker_resolves_the_profile(client):
    # OWNER has an editor share on OTHER's agent: the run broker resolves it, the desktop does not.
    resp = await client.post("/api/run-broker/desktop/observe",
                             headers=_hdr(OWNER, **{"X-Hermes-Agent-Id": "webui:ou_other:shared"}), json={})
    assert resp.status == 403
    assert "owner-only" in (await resp.json())["message"]


async def test_desktop_disabled_profile_is_403_and_never_ensures(client, world, monkeypatch):
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.routing import RoutingTable

    table = RoutingTable(world["root"] / "routing.db")
    table.upsert(user_id="u-plain", profile_name="plain", open_id="ou_plain", provenance="sync")
    table.close()
    router_mod.override_routing_table(world["root"] / "routing.db")
    ensured = []
    monkeypatch.setattr(ds, "ensure", lambda d: ensured.append(d))
    for path in ("observe", "ensure"):
        resp = await client.post(f"/api/run-broker/desktop/{path}", headers=_hdr("ou_plain"), json={})
        assert resp.status == 403
        assert (await resp.json()) == {"error": "desktop_disabled"}
    assert ensured == []


# --- observe / ensure ---------------------------------------------------------------------------


async def test_observe_mints_viewer_and_returns_public_lease(client, world):
    world["running"]["value"] = False
    body = await _observe(client)
    assert body["enabled"] is True and body["running"] is False
    assert body["lease"]["holder"] == "agent" and body["lease"]["viewer_id"] is None
    assert len(body["viewer_id"]) >= 20
    assert body["viewer_id"] != (await _observe(client))["viewer_id"]


async def test_ensure_runs_sandbox_ensure_off_the_loop_and_maps_errors(client, world, monkeypatch):
    import threading

    seen = []

    def fake_ensure(decision):
        seen.append((decision.profile_name, threading.current_thread() is threading.main_thread()))

    monkeypatch.setattr(ds, "ensure", fake_ensure)
    resp = await client.post("/api/run-broker/desktop/ensure", headers=_hdr(), json={})
    assert resp.status == 200
    assert seen == [("mine", False)]
    assert set(await resp.json()) == {"enabled", "running", "lease", "viewer_id"}

    def quota(decision):
        raise ds.DesktopSandboxError("desktop_quota_exhausted", "full", user_message=ds.QUOTA_USER_MESSAGE)

    monkeypatch.setattr(ds, "ensure", quota)
    resp = await client.post("/api/run-broker/desktop/ensure", headers=_hdr(), json={})
    assert resp.status == 503
    assert await resp.json() == {"error": "desktop_quota_exhausted", "message": ds.QUOTA_USER_MESSAGE}


# --- viewer registry ----------------------------------------------------------------------------


def test_viewer_ttl_slides_with_use_and_sockets_pin():
    now = [0.0]
    reg = dv.ViewerRegistry(ttl_s=600, max_per_profile=4, clock=lambda: now[0])
    vid = reg.mint(owner=OWNER, profile_name="p", profile_home="/h")
    now[0] = 590
    reg.check(vid, owner=OWNER, profile_home="/h")  # use slides the TTL
    now[0] = 1180
    reg.check(vid, owner=OWNER, profile_home="/h")
    reg.claim_socket(vid, owner=OWNER, profile_home="/h")
    now[0] = 5000
    reg.check(vid, owner=OWNER, profile_home="/h")  # an attached socket keeps it alive
    assert reg.release_socket(vid, clean=False) is False
    now[0] = 5601
    with pytest.raises(dv.ViewerError) as exc:
        reg.check(vid, owner=OWNER, profile_home="/h")
    assert (exc.value.code, exc.value.ws_code) == ("viewer_expired", dv.CLOSE_BAD_VIEWER)


def test_viewer_bound_to_owner_and_profile():
    reg = dv.ViewerRegistry()
    vid = reg.mint(owner=OWNER, profile_name="p", profile_home="/h")
    for owner, home in ((OTHER, "/h"), (OWNER, "/other")):
        with pytest.raises(dv.ViewerError) as exc:
            reg.check(vid, owner=owner, profile_home=home)
        assert (exc.value.code, exc.value.ws_code) == ("viewer_forbidden", dv.CLOSE_NOT_ALLOWED)


def test_viewer_cap_drops_oldest_detached_then_refuses():
    now = [0.0]
    reg = dv.ViewerRegistry(max_per_profile=4, clock=lambda: now[0])
    ids = []
    for i in range(4):
        now[0] = i
        ids.append(reg.mint(owner=OWNER, profile_name="p", profile_home="/h"))
    reg.claim_socket(ids[0], owner=OWNER, profile_home="/h")
    now[0] = 10
    reg.mint(owner=OWNER, profile_name="p", profile_home="/h")
    reg.check(ids[0], owner=OWNER, profile_home="/h")  # attached: survived
    with pytest.raises(dv.ViewerError):
        reg.check(ids[1], owner=OWNER, profile_home="/h")  # oldest detached: dropped
    reg.mint(owner=OWNER, profile_name="q", profile_home="/other")  # other profiles do not count
    for v in [v for v in reg._viewers.values() if v.profile_home == "/h" and v.sockets == 0]:
        reg.claim_socket(v.viewer_id, owner=OWNER, profile_home="/h")
    with pytest.raises(dv.ViewerError) as exc:
        reg.mint(owner=OWNER, profile_name="p", profile_home="/h")
    assert exc.value.code == "too_many_viewers"


# --- lease routes -------------------------------------------------------------------------------


async def test_acquire_release_drive_core_lease_and_audit(client, world):
    home = world["profiles"] / "mine"
    vid = (await _observe(client))["viewer_id"]
    resp = await client.post("/api/run-broker/desktop/lease/acquire", headers=_hdr(), json={"viewer_id": vid})
    assert resp.status == 200
    public = (await resp.json())["lease"]
    assert public["holder"] == "human" and public["viewer_id"] is None and public["viewer_hash"]
    on_disk = _lease(home)
    assert (on_disk["holder"], on_disk["viewer_id"], on_disk["epoch"]) == ("human", vid, 1)

    resp = await client.post("/api/run-broker/desktop/lease/release", headers=_hdr(), json={"viewer_id": vid})
    assert (await resp.json())["lease"]["holder"] == "agent"
    assert _lease(home)["epoch"] == 2

    events = [e for e in _audit(world) if e["event_type"].startswith("desktop.lease.")]
    assert [(e["event_type"], e["epoch"], e["lease_kind"], e["decision"]) for e in events] == [
        ("desktop.lease.acquire", "1", "human", "changed"),
        ("desktop.lease.release", "2", "agent", "changed"),
    ]
    assert all(e["profile"] == "mine" and e["open_id_hash"] and len(e["viewer_hash"]) == 12 for e in events)
    assert vid not in world["audit"].read_text()


async def test_release_by_non_holder_is_ignored_unless_forced(client, world):
    home = world["profiles"] / "mine"
    a = (await _observe(client))["viewer_id"]
    b = (await _observe(client))["viewer_id"]
    await client.post("/api/run-broker/desktop/lease/acquire", headers=_hdr(), json={"viewer_id": a})
    resp = await client.post("/api/run-broker/desktop/lease/release", headers=_hdr(), json={"viewer_id": b})
    assert (await resp.json())["lease"]["holder"] == "human"
    resp = await client.post("/api/run-broker/desktop/lease/release", headers=_hdr(),
                             json={"viewer_id": b, "force": True})
    assert (await resp.json())["lease"]["holder"] == "agent"
    assert _lease(home)["holder"] == "agent"


async def test_lease_routes_refuse_foreign_or_unknown_viewer(client, world):
    theirs = (await _observe(client, OTHER))["viewer_id"]
    resp = await client.post("/api/run-broker/desktop/lease/acquire", headers=_hdr(), json={"viewer_id": theirs})
    assert resp.status == 403 and (await resp.json())["error"] == "viewer_forbidden"
    resp = await client.post("/api/run-broker/desktop/lease/acquire", headers=_hdr(), json={"viewer_id": "nope"})
    assert resp.status == 403 and (await resp.json())["error"] == "viewer_expired"
    assert _lease(world["profiles"] / "mine")["holder"] == "agent"
    assert _lease(world["profiles"] / "theirs")["holder"] == "agent"


# --- WebSocket bridge ---------------------------------------------------------------------------


async def _ws(client, vid: str, owner: str = OWNER):
    return await client.ws_connect(f"/api/run-broker/desktop/ws?viewer_id={vid}", headers=_hdr(owner))


async def _close_code(ws) -> int:
    from aiohttp import WSMsgType

    while True:
        msg = await asyncio.wait_for(ws.receive(), 5)
        if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING):
            return ws.close_code


@pytest.fixture
async def xvnc(world):
    fake = FakeXvnc(world["profiles"] / "mine" / "bot-desktop" / "rfb.sock")
    await fake.start()
    yield fake
    fake.server.close()


async def test_ws_close_codes_before_the_pump(client, world):
    vid = (await _observe(client))["viewer_id"]
    ws = await _ws(client, vid)  # no rfb.sock yet
    assert await _close_code(ws) == dv.CLOSE_DESKTOP_GONE
    ws = await _ws(client, "unknown")
    assert await _close_code(ws) == dv.CLOSE_BAD_VIEWER
    theirs = (await _observe(client, OTHER))["viewer_id"]
    ws = await _ws(client, theirs)
    assert await _close_code(ws) == dv.CLOSE_NOT_ALLOWED
    ws = await client.ws_connect(f"/api/run-broker/desktop/ws?viewer_id={vid}", headers=AUTH)
    assert await _close_code(ws) == dv.CLOSE_NOT_ALLOWED


async def test_ws_filters_input_until_lease_then_passes_it(client, world, xvnc):
    vid = (await _observe(client))["viewer_id"]
    ws = await _ws(client, vid)
    assert (await asyncio.wait_for(ws.receive_bytes(), 5)) == RFB_VERSION
    await ws.send_bytes(CLIENT_HANDSHAKE + POINTER + FB_REQUEST)
    await xvnc.wait_for(len(CLIENT_HANDSHAKE) + len(FB_REQUEST))
    # ClientInit forced shared; the pointer event was dropped, the update request passed.
    assert bytes(xvnc.received) == RFB_VERSION + b"\x01" + b"\x01" + FB_REQUEST

    await client.post("/api/run-broker/desktop/lease/acquire", headers=_hdr(), json={"viewer_id": vid})
    await ws.send_bytes(POINTER)
    await xvnc.wait_for(len(CLIENT_HANDSHAKE) + len(FB_REQUEST) + len(POINTER))
    assert bytes(xvnc.received).endswith(POINTER)
    await ws.close()


async def test_ws_text_frame_is_protocol_error(client, xvnc):
    vid = (await _observe(client))["viewer_id"]
    ws = await _ws(client, vid)
    await ws.send_str("hello")
    assert await _close_code(ws) == dv.CLOSE_PROTOCOL


async def test_second_viewer_takeover_evicts_first_with_4000(client, world, xvnc):
    a = (await _observe(client))["viewer_id"]
    b = (await _observe(client))["viewer_id"]
    await client.post("/api/run-broker/desktop/lease/acquire", headers=_hdr(), json={"viewer_id": a})
    ws_a = await _ws(client, a)
    ws_b = await _ws(client, b)
    await asyncio.wait_for(ws_a.receive_bytes(), 5)
    await client.post("/api/run-broker/desktop/lease/acquire", headers=_hdr(), json={"viewer_id": b})
    assert await _close_code(ws_a) == dv.CLOSE_CONTROL_TAKEN
    assert not ws_b.closed
    await ws_b.close()


async def test_takeover_from_another_process_is_seen_by_polling(client, world, xvnc):
    from tools.bot_desktop import lease as core_lease

    home = world["profiles"] / "mine"
    a = (await _observe(client))["viewer_id"]
    await client.post("/api/run-broker/desktop/lease/acquire", headers=_hdr(), json={"viewer_id": a})
    ws_a = await _ws(client, a)
    await asyncio.wait_for(ws_a.receive_bytes(), 5)
    # Write the file the way another process would: no in-process on_change callback fires.
    path = home / "bot-desktop" / "lease.json"
    path.write_text(json.dumps({"holder": "human", "viewer_id": "elsewhere", "epoch": 9}))
    assert core_lease.get(profile_key=str(home)).viewer_id == "elsewhere"
    assert await _close_code(ws_a) == dv.CLOSE_CONTROL_TAKEN


async def test_clean_close_releases_but_dropped_link_keeps_lease(client, world, xvnc):
    home = world["profiles"] / "mine"
    vid = (await _observe(client))["viewer_id"]
    await client.post("/api/run-broker/desktop/lease/acquire", headers=_hdr(), json={"viewer_id": vid})
    ws = await _ws(client, vid)
    await asyncio.wait_for(ws.receive_bytes(), 5)
    ws._writer.transport.abort()  # laptop lid: no close frame
    await asyncio.sleep(0.5)
    assert _lease(home)["holder"] == "human"

    ws = await _ws(client, vid)  # reconnect into the same lease, then close the window
    await asyncio.wait_for(ws.receive_bytes(), 5)
    await ws.close(code=1000)
    for _ in range(50):
        if _lease(home)["holder"] == "agent":
            break
        await asyncio.sleep(0.05)
    assert _lease(home)["holder"] == "agent"
    assert [e["event_type"] for e in _audit(world)][-1] == "desktop.lease.release"


async def test_rfb_eof_closes_viewer_with_4001(client, world, xvnc):
    vid = (await _observe(client))["viewer_id"]
    ws = await _ws(client, vid)
    await asyncio.wait_for(ws.receive_bytes(), 5)
    await xvnc.hang_up()
    assert await _close_code(ws) == dv.CLOSE_DESKTOP_GONE


async def test_attached_viewer_stamps_last_used(client, world, xvnc, monkeypatch):
    monkeypatch.setattr(dv, "ACTIVITY_STAMP_S", 0.0)
    home = world["profiles"] / "mine"
    state = ds.profile_state_dir(world["shared"], home)
    vid = (await _observe(client))["viewer_id"]
    ws = await _ws(client, vid)
    await asyncio.wait_for(ws.receive_bytes(), 5)
    for _ in range(40):
        if ds.read_last_used(state) is not None:
            break
        await asyncio.sleep(0.05)
    assert ds.read_last_used(state) is not None
    await ws.close()


# --- idle sweep ---------------------------------------------------------------------------------


def test_idle_sweep_keeps_container_while_a_human_holds_the_lease(monkeypatch, tmp_path):
    from tools.bot_desktop import lease as core_lease

    shared = tmp_path / ".hermes"
    held = shared / "profiles" / "held"
    idle = shared / "profiles" / "idle"
    now = 1_000_000.0
    for home in (held, idle):
        home.mkdir(parents=True)
        state = ds._ensure_state_dir(ds.profile_state_dir(shared, home))
        ds.touch_last_used(state, now=now - 31 * 60)
    core_lease.acquire("viewer-1", profile_key=str(held))
    podman = tmp_path / "podman"
    podman.write_text("#!/bin/sh\n")
    podman.chmod(0o755)
    calls = []

    def fake_run(podman_bin, args, *, timeout=None, check=False):
        calls.append(args)
        if args[0] == "ps":
            rows = [{"Names": [f"hermes-p-{h.name}"], "Labels": {ds.LABEL_PROFILE_HOME: str(h)}} for h in (held, idle)]
            return subprocess.CompletedProcess(args, 0, json.dumps(rows), "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(ds, "_run_podman", fake_run)
    assert ds.idle_stop_sweep(podman_bin=str(podman), shared_home=shared, now=now) == ["hermes-p-idle"]
    core_lease.release("viewer-1", profile_key=str(held))
    assert ds.idle_stop_sweep(podman_bin=str(podman), shared_home=shared, now=now) == ["hermes-p-held", "hermes-p-idle"]


# --- review round 1 regressions -----------------------------------------------------------------


@pytest.mark.parametrize("bad", ["false", 1, {}, "true", 0, None])
async def test_release_force_must_be_a_json_boolean(client, world, bad):
    home = world["profiles"] / "mine"
    a = (await _observe(client))["viewer_id"]
    b = (await _observe(client))["viewer_id"]
    await client.post("/api/run-broker/desktop/lease/acquire", headers=_hdr(), json={"viewer_id": a})
    resp = await client.post("/api/run-broker/desktop/lease/release", headers=_hdr(),
                             json={"viewer_id": b, "force": bad})
    assert resp.status == 400 and (await resp.json())["error"] == "invalid_force"
    assert _lease(home)["holder"] == "human"
    resp = await client.post("/api/run-broker/desktop/lease/release", headers=_hdr(),
                             json={"viewer_id": b, "force": False})
    assert resp.status == 200 and (await resp.json())["lease"]["holder"] == "human"


async def _wait_holder(home: Path, holder: str) -> str:
    for _ in range(60):
        if _lease(home)["holder"] == holder:
            break
        await asyncio.sleep(0.05)
    return _lease(home)["holder"]


async def test_clean_close_releases_only_when_last_socket_of_the_viewer_closes(client, world, xvnc):
    home = world["profiles"] / "mine"
    vid = (await _observe(client))["viewer_id"]
    await client.post("/api/run-broker/desktop/lease/acquire", headers=_hdr(), json={"viewer_id": vid})
    first, second = await _ws(client, vid), await _ws(client, vid)  # duplicate tab of the same viewer
    await asyncio.wait_for(first.receive_bytes(), 5)
    await asyncio.wait_for(second.receive_bytes(), 5)
    await first.close(code=1000)
    await asyncio.sleep(0.5)
    assert _lease(home)["holder"] == "human"  # the other tab is still driving
    await second.close(code=1000)
    assert await _wait_holder(home, "agent") == "agent"


def test_registry_refuses_new_socket_while_a_clean_close_hands_back():
    reg = dv.ViewerRegistry()
    vid = reg.mint(owner=OWNER, profile_name="p", profile_home="/h")
    reg.claim_socket(vid, owner=OWNER, profile_home="/h")
    reg.claim_socket(vid, owner=OWNER, profile_home="/h")
    assert reg.release_socket(vid, clean=True) is False  # not the last socket
    assert reg.release_socket(vid, clean=True) is True
    with pytest.raises(dv.ViewerError) as exc:
        reg.claim_socket(vid, owner=OWNER, profile_home="/h")
    assert (exc.value.code, exc.value.ws_code) == ("viewer_releasing", dv.CLOSE_BAD_VIEWER)
    reg.finish_release(vid)
    reg.claim_socket(vid, owner=OWNER, profile_home="/h")
    with pytest.raises(RuntimeError):
        reg.release_socket("never-minted", clean=True)


def test_claim_is_atomic_against_concurrent_mints():
    import threading

    for _ in range(50):
        reg = dv.ViewerRegistry(max_per_profile=4)
        ids = [reg.mint(owner=OWNER, profile_name="p", profile_home="/h") for _ in range(4)]
        target = ids[0]  # least recently used: the first eviction candidate
        barrier = threading.Barrier(5)
        claimed = []

        def claim():
            barrier.wait()
            try:
                reg.claim_socket(target, owner=OWNER, profile_home="/h")
                claimed.append(True)
            except dv.ViewerError:
                claimed.append(False)

        def mint():
            barrier.wait()
            try:
                reg.mint(owner=OWNER, profile_name="p", profile_home="/h")
            except dv.ViewerError:
                pass

        threads = [threading.Thread(target=claim)] + [threading.Thread(target=mint) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        mine = [v for v in reg._viewers.values() if v.profile_home == "/h"]
        assert len(mine) <= 4
        if claimed == [True]:
            # A claimed viewer is attached: never evicted, and its socket count is exact.
            assert reg._viewers[target].sockets == 1
        else:
            assert target not in reg._viewers


async def test_ws_claim_survives_concurrent_observes_during_handshake(client, world, xvnc, monkeypatch):
    import threading

    vid = (await _observe(client))["viewer_id"]
    for _ in range(3):
        await _observe(client)
    entered, go = threading.Event(), threading.Event()
    real = dv.desktop_decision

    def slow_decision(home):
        entered.set()
        go.wait(5)
        return real(home)

    monkeypatch.setattr(dv, "desktop_decision", slow_decision)
    connect = asyncio.create_task(_ws(client, vid))
    await asyncio.to_thread(entered.wait, 5)
    monkeypatch.setattr(dv, "desktop_decision", real)
    for _ in range(4):  # profile is full: each mint must evict a DETACHED viewer, never this one
        await _observe(client)
    go.set()
    ws = await connect
    await asyncio.wait_for(ws.receive_bytes(), 5)
    resp = await client.post("/api/run-broker/desktop/lease/acquire", headers=_hdr(), json={"viewer_id": vid})
    assert resp.status == 200
    assert sum(1 for v in dv.VIEWERS._viewers.values() if v.profile_home.endswith("/mine")) == 4
    await ws.close()


def test_idle_sweep_rechecks_lease_under_the_profile_lock(monkeypatch, tmp_path):
    """A takeover that lands between the sweep's idle check and its profile lock is seen, not stopped under."""
    from contextlib import contextmanager

    from tools.bot_desktop import lease as core_lease

    shared = tmp_path / ".hermes"
    home = shared / "profiles" / "raced"
    home.mkdir(parents=True)
    now = 1_000_000.0
    state = ds._ensure_state_dir(ds.profile_state_dir(shared, home))
    ds.touch_last_used(state, now=now - 31 * 60)
    podman = tmp_path / "podman"
    podman.write_text("#!/bin/sh\n")
    podman.chmod(0o755)
    real_flock = ds._flock

    @contextmanager
    def fenced_flock(path, *, timeout_s, what):
        with real_flock(path, timeout_s=timeout_s, what=what):
            if what.startswith("idle sweep"):
                core_lease.acquire("raced-viewer", profile_key=str(home))  # the user won the race
            yield

    stops = []

    def fake_run(podman_bin, args, *, timeout=None, check=False):
        if args[0] == "ps":
            rows = [{"Names": ["hermes-p-raced"], "Labels": {ds.LABEL_PROFILE_HOME: str(home)}}]
            return subprocess.CompletedProcess(args, 0, json.dumps(rows), "")
        stops.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(ds, "_flock", fenced_flock)
    monkeypatch.setattr(ds, "_run_podman", fake_run)
    assert ds.idle_stop_sweep(podman_bin=str(podman), shared_home=shared, now=now) == []
    assert stops == []


async def test_acquire_waits_for_the_sweep_lock_and_fails_closed_if_it_stopped_the_container(client, world):
    import threading

    home = world["profiles"] / "mine"
    decision = dv.desktop_decision(home)
    state = ds._ensure_state_dir(decision.state_dir)
    vid = (await _observe(client))["viewer_id"]
    held, done = threading.Event(), threading.Event()

    def sweep_holding_lock():
        with ds._flock(state / ds.PROFILE_LOCK_NAME, timeout_s=1.0, what="idle sweep test"):
            held.set()
            done.wait(5)
            world["running"]["value"] = False  # the sweep's podman stop

    t = threading.Thread(target=sweep_holding_lock)
    t.start()
    await asyncio.to_thread(held.wait, 5)
    req = asyncio.create_task(client.post("/api/run-broker/desktop/lease/acquire", headers=_hdr(),
                                          json={"viewer_id": vid}))
    await asyncio.sleep(0.4)
    assert not req.done() and _lease(home)["holder"] == "agent"  # blocked on the lock
    done.set()
    resp = await req
    t.join()
    assert resp.status == 503 and (await resp.json())["error"] == "desktop_not_running"
    assert _lease(home)["holder"] == "agent"  # no human lease left on a stopped screen


async def test_acquire_stamps_last_used_under_the_lock(client, world):
    home = world["profiles"] / "mine"
    state = ds.profile_state_dir(world["shared"], home)
    vid = (await _observe(client))["viewer_id"]
    resp = await client.post("/api/run-broker/desktop/lease/acquire", headers=_hdr(), json={"viewer_id": vid})
    assert resp.status == 200
    assert ds.read_last_used(state) is not None


async def test_release_still_works_after_desktop_is_disabled(client, world):
    home = world["profiles"] / "mine"
    vid = (await _observe(client))["viewer_id"]
    await client.post("/api/run-broker/desktop/lease/acquire", headers=_hdr(), json={"viewer_id": vid})
    world["enabled"].discard("mine")
    resp = await client.post("/api/run-broker/desktop/lease/acquire", headers=_hdr(), json={"viewer_id": vid})
    assert resp.status == 403 and (await resp.json()) == {"error": "desktop_disabled"}
    resp = await client.post("/api/run-broker/desktop/lease/release", headers=_hdr(), json={"viewer_id": vid})
    assert resp.status == 200 and (await resp.json())["lease"]["holder"] == "agent"
    assert _lease(home)["holder"] == "agent"
    # Disabled release still checks the viewer and the owner.
    resp = await client.post("/api/run-broker/desktop/lease/release", headers=_hdr(), json={"viewer_id": "forged"})
    assert resp.status == 403
    resp = await client.post("/api/run-broker/desktop/lease/release", headers=_hdr(OTHER), json={"viewer_id": vid})
    assert resp.status == 403
