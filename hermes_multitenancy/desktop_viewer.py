"""Bot Desktop viewer bridge on the run broker: observe / ensure / lease / RFB-over-WebSocket.

The WebUI authenticates the browser (Feishu cookie) and forwards to these routes with the broker
Bearer key and the verified ``X-Hermes-Owner-Open-Id``; MT resolves the profile and is the single
security gate. Only the profile's OWNER (sync root or owned agent) may view or drive its screen — a
share grantee never sees a desktop the owner may be typing a credential into.

Input gating is core's own: ``tools.bot_desktop.lease`` (who drives, on disk under an fcntl lock,
shared with ``computer_use`` inside the container) and ``tools.bot_desktop.rfb_filter.RfbClientFilter``
(per-message parse of the client stream; input passes only from the lease holder). The pump follows
core ``hermes_cli/web_routers/display.py::_bridge`` rewritten for aiohttp, with two additions: the lease
file is polled every 250 ms for evictions too (a takeover from ANOTHER process lands without an
in-process callback), and an RFB EOF closes the viewer with 4001 (desktop gone) instead of 1000.

``viewer_id`` is minted here (``secrets.token_urlsafe(16)``) and is a capability: whoever presents it
co-drives the lease. It lives only in this process, bound to (owner, profile home), with a sliding
10-minute TTL refreshed by use and by an attached socket; at most 4 live viewers per profile. One id may
carry several sockets (duplicate tabs, overlapping reconnects); only a clean close of its LAST socket
hands the lease back.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

OWNER_HEADER = "X-Hermes-Owner-Open-Id"

VIEWER_TTL_S = 600.0
MAX_VIEWERS_PER_PROFILE = 4
OBSERVE_TIMEOUT_S = 10.0

READ_CHUNK = 64 * 1024
WS_MAX_MSG_SIZE = 1024 * 1024  # > rfb_filter's 256 KiB clipboard cap; a bigger frame is refused by aiohttp
LEASE_REFRESH_S = 0.25
ACTIVITY_STAMP_S = 60.0

CLOSE_CONTROL_TAKEN = 4000
CLOSE_DESKTOP_GONE = 4001
CLOSE_BAD_VIEWER = 4401
CLOSE_NOT_ALLOWED = 4403
CLOSE_PROTOCOL = 1003
CLEAN_CLOSE = frozenset({1000, 1001})


class ViewerError(Exception):
    """A viewer_id that cannot be used: ``code`` is the HTTP error slug, ``ws_code`` the close code."""

    def __init__(self, code: str, ws_code: int, message: str):
        super().__init__(message)
        self.code = code
        self.ws_code = ws_code


@dataclass
class Viewer:
    viewer_id: str
    owner: str
    profile_name: str
    profile_home: str
    created: float
    touched: float
    sockets: int = 0
    #: A clean close of its last socket is handing the lease back; no new socket until that is done.
    releasing: bool = False


class ViewerRegistry:
    """In-memory viewer_id → (owner, profile home). Process-local by design: the bridge that minted
    the id is the only one that can pump its socket."""

    def __init__(self, *, ttl_s: float = VIEWER_TTL_S, max_per_profile: int = MAX_VIEWERS_PER_PROFILE,
                 clock: Callable[[], float] = time.monotonic):
        self._ttl = ttl_s
        self._max = max_per_profile
        self._clock = clock
        self._lock = threading.Lock()
        self._viewers: dict[str, Viewer] = {}

    def _expired(self, viewer: Viewer, now: float) -> bool:
        # An attached socket keeps its viewer alive regardless of the clock.
        return viewer.sockets == 0 and now - viewer.touched > self._ttl

    def _prune(self, now: float) -> None:
        for vid in [v.viewer_id for v in self._viewers.values() if self._expired(v, now)]:
            del self._viewers[vid]

    def mint(self, *, owner: str, profile_name: str, profile_home: str) -> str:
        """New viewer for this profile. A full profile drops its least-recently-used DETACHED viewer;
        when all of them have a live socket the mint is refused (``too_many_viewers``)."""
        with self._lock:
            now = self._clock()
            self._prune(now)
            mine = [v for v in self._viewers.values() if v.profile_home == profile_home]
            if len(mine) >= self._max:
                detached = sorted((v for v in mine if v.sockets == 0), key=lambda v: v.touched)
                if not detached:
                    raise ViewerError("too_many_viewers", CLOSE_NOT_ALLOWED,
                                      f"{self._max} viewers are already attached to this desktop")
                del self._viewers[detached[0].viewer_id]
            vid = secrets.token_urlsafe(16)
            self._viewers[vid] = Viewer(vid, owner, profile_name, profile_home, now, now)
            return vid

    def _checked(self, viewer_id: str, owner: str, profile_home: str) -> Viewer:
        """Caller holds the lock."""
        now = self._clock()
        self._prune(now)
        viewer = self._viewers.get(viewer_id or "")
        if viewer is None:
            raise ViewerError("viewer_expired", CLOSE_BAD_VIEWER, "viewer_id unknown or expired; observe again")
        if viewer.owner != owner or viewer.profile_home != profile_home:
            raise ViewerError("viewer_forbidden", CLOSE_NOT_ALLOWED, "viewer_id does not belong to this owner")
        viewer.touched = now
        return viewer

    def check(self, viewer_id: str, *, owner: str, profile_home: str) -> Viewer:
        """The viewer, touched, if it is live and bound to exactly this owner and profile."""
        with self._lock:
            return self._checked(viewer_id, owner, profile_home)

    def touch(self, viewer_id: str) -> None:
        with self._lock:
            viewer = self._viewers.get(viewer_id)
            if viewer is not None:
                viewer.touched = self._clock()

    def claim_socket(self, viewer_id: str, *, owner: str, profile_home: str) -> None:
        """Check and count one socket in ONE critical section: from here on the viewer is attached, so
        a concurrent mint can never evict it. Pair every successful claim with :meth:`release_socket`."""
        with self._lock:
            viewer = self._checked(viewer_id, owner, profile_home)
            if viewer.releasing:
                raise ViewerError("viewer_releasing", CLOSE_BAD_VIEWER,
                                  "this viewer is handing control back; observe again")
            viewer.sockets += 1

    def release_socket(self, viewer_id: str, *, clean: bool) -> bool:
        """Uncount one socket. True means the caller must hand the lease back and then call
        :meth:`finish_release`: this was the viewer's LAST socket and it closed cleanly. Until then the
        viewer refuses new sockets, so a reconnect cannot land on a lease being released under it."""
        with self._lock:
            viewer = self._viewers.get(viewer_id)
            if viewer is None or viewer.sockets <= 0:
                # Claimed viewers are never pruned or evicted; reaching here is a bookkeeping bug.
                raise RuntimeError(f"release_socket without a claimed socket for viewer {_viewer_hash(viewer_id)}")
            viewer.sockets -= 1
            viewer.touched = self._clock()
            if clean and viewer.sockets == 0:
                viewer.releasing = True
                return True
            return False

    def finish_release(self, viewer_id: str) -> None:
        with self._lock:
            viewer = self._viewers.get(viewer_id)
            if viewer is not None:
                viewer.releasing = False

    def clear(self) -> None:
        with self._lock:
            self._viewers.clear()


VIEWERS = ViewerRegistry()


@dataclass(frozen=True)
class Target:
    """The profile a request is about, already authorized for ``owner``."""

    owner: str
    profile_name: str
    profile_home: Path


def _viewer_hash(viewer_id: str) -> str:
    return hashlib.sha256(viewer_id.encode("utf-8")).hexdigest()[:12]


def desktop_decision(profile_home: Path):
    """The gateway's own decision for this profile (shared + profile config, resolved separately)."""
    from .agent_real._core import _desktop_decision_for_profile

    return _desktop_decision_for_profile(profile_home)


def owner_owns_profile(owner: str, profile_name: str) -> bool:
    """Owner-only: the owner's sync root, or a route row whose owner_open_id is ``owner``. Shares do
    not count. No routing table → False (fail closed)."""
    from . import router as router_mod

    table = router_mod._get_routing_table()
    if table is None:
        return False
    root = table.resolve_owner_root(owner)
    if root is not None and root.profile_name == profile_name:
        return True
    return any(row.profile_name == profile_name for row in table.list_agents_for_owner(owner))


def _container_running(decision) -> bool:
    """Container State.Status == running AND rfb.sock is a socket. Two podman-free signals would lie
    after a crash (stale socket inode), so inspect is the authority; the socket says the screen is up."""
    from . import desktop_sandbox as ds

    podman = ds._podman_available(decision.podman_bin)
    if podman is None:
        return False
    try:
        info = ds._inspect_container(podman, decision.container_name, timeout=OBSERVE_TIMEOUT_S)
    except Exception as exc:  # timeout / OSError / DesktopSandboxError: report "not running", never 500
        logger.warning("[multitenancy] desktop viewer: inspect %s failed: %s", decision.container_name, exc)
        return False
    return info is not None and info.status == "running" and ds._socket_present(decision.rfb_socket)


def _audit_lease(event_type: str, target: Target, viewer_id: str, before_epoch: int, lease) -> None:
    from .security_audit import append_security_event

    append_security_event(
        event_type=event_type,
        profile=target.profile_name,
        open_id=target.owner,
        epoch=str(lease.epoch),
        viewer_hash=_viewer_hash(viewer_id),
        lease_kind=lease.holder,
        decision="changed" if lease.epoch != before_epoch else "unchanged",
    )


class DesktopGone(RuntimeError):
    """The container is not running: a takeover would leave a human lease on a screen nobody sees."""


ACQUIRE_LOCK_WAIT_S = 15.0


def acquire_lease(target: Target, viewer_id: str, decision):
    """Take the lease under the same profile lock the idle sweep stops containers under: confirm the
    container still runs, stamp ``last_used``, then acquire. A sweep that won the lock first has
    stopped the container and the takeover fails with :class:`DesktopGone`, leaving no human lease.
    A lock held past ``ACQUIRE_LOCK_WAIT_S`` (an ensure in flight) raises DesktopSandboxError."""
    from tools.bot_desktop import lease as _lease

    from . import desktop_sandbox as ds

    home = str(target.profile_home)
    state_dir = ds._ensure_state_dir(decision.state_dir)
    with ds._flock(state_dir / ds.PROFILE_LOCK_NAME, timeout_s=ACQUIRE_LOCK_WAIT_S,
                   what=f"lease acquire {decision.container_name}"):
        if not _container_running(decision):
            raise DesktopGone(f"{decision.container_name} is not running")
        ds.touch_last_used(state_dir)
        before = _lease.get(profile_key=home).epoch
        result = _lease.acquire(viewer_id, profile_key=home, reason="webui takeover")
    _audit_lease("desktop.lease.acquire", target, viewer_id, before, result)
    return result


def release_lease(target: Target, viewer_id: str, *, force: bool = False):
    """``force`` hands control back whichever of the owner's viewers holds it."""
    from tools.bot_desktop import lease as _lease

    home = str(target.profile_home)
    before = _lease.get(profile_key=home).epoch
    result = _lease.release(None if force else viewer_id, profile_key=home)
    _audit_lease("desktop.lease.release", target, viewer_id, before, result)
    return result


def should_evict(held: dict, lease, viewer_id: str) -> bool:
    """Core ``display._should_evict``: a viewer that held control during this connection and lost it
    to ANOTHER human is kicked (4000); a hand-back to the agent resets its memory of having held."""
    from tools.bot_desktop import lease as _lease

    if lease.holder != _lease.HUMAN:
        held["ever"] = False
        return False
    if lease.viewer_id == viewer_id:
        held["ever"] = True
        return False
    return bool(held["ever"])


def register_routes(
    app: Any,
    *,
    authorize: Callable[[Any], bool],
    resolve_profile: Callable[[Any, dict], tuple[Optional[str], Optional[str]]],
    profile_home: Callable[[str], Path],
    registry: ViewerRegistry = VIEWERS,
) -> None:
    from aiohttp import web

    from . import desktop_sandbox as ds

    def _target(request, payload: dict) -> Target:
        """Authorized target or PermissionError (→ 403)."""
        owner = str(request.headers.get(OWNER_HEADER, "") or "").strip()
        if not owner:
            raise PermissionError("owner identity required (X-Hermes-Owner-Open-Id)")
        profile_name, err = resolve_profile(request, payload)
        if err or not profile_name:
            raise PermissionError(err or "profile not resolvable for owner")
        if not owner_owns_profile(owner, profile_name):
            raise PermissionError(f"desktop of profile '{profile_name}' is owner-only")
        return Target(owner, profile_name, Path(profile_home(profile_name)).expanduser().resolve())

    async def _payload(request) -> dict:
        if not request.can_read_body:
            return {}
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            raise web.HTTPBadRequest(text=json.dumps({"error": "invalid_json"}), content_type="application/json")
        return body if isinstance(body, dict) else {}

    async def _prologue(request, *, require_enabled: bool = True
                        ) -> tuple[Optional[web.Response], Optional[Target], dict, Any]:
        """Bearer, owner, profile and (unless ``require_enabled`` is False) desktop-enabled checks."""
        if not authorize(request):
            return web.json_response({"error": "unauthorized"}, status=401), None, {}, None
        payload = await _payload(request)
        try:
            target = _target(request, payload)
        except PermissionError as exc:
            return web.json_response({"error": "forbidden", "message": str(exc)}, status=403), None, payload, None
        decision = await asyncio.to_thread(desktop_decision, target.profile_home)
        if require_enabled and not decision.enabled:
            return web.json_response({"error": "desktop_disabled"}, status=403), target, payload, decision
        return None, target, payload, decision

    async def _observe_body(target: Target, decision, *, viewer_id: Optional[str] = None) -> dict:
        from tools.bot_desktop import lease as _lease

        running = await asyncio.to_thread(_container_running, decision)
        lease = await asyncio.to_thread(_lease.get, str(target.profile_home))
        if viewer_id is None:
            viewer_id = registry.mint(owner=target.owner, profile_name=target.profile_name,
                                      profile_home=str(target.profile_home))
        return {"enabled": True, "running": running, "lease": _lease.public_view(lease), "viewer_id": viewer_id}

    def _viewer_error(exc: ViewerError) -> web.Response:
        status = 429 if exc.code == "too_many_viewers" else 403
        return web.json_response({"error": exc.code, "message": str(exc)}, status=status)

    async def handle_observe(request):
        early, target, _, decision = await _prologue(request)
        if early is not None:
            return early
        try:
            return web.json_response(await _observe_body(target, decision))
        except ViewerError as exc:
            return _viewer_error(exc)

    async def handle_ensure(request):
        early, target, payload, decision = await _prologue(request)
        if early is not None:
            return early
        try:
            await asyncio.to_thread(ds.ensure, decision)
        except ds.DesktopSandboxError as exc:
            logger.warning("[multitenancy] desktop viewer ensure failed profile=%s reason=%s: %s",
                           target.profile_name, exc.reason, exc)
            return web.json_response(
                {"error": exc.reason, "message": exc.user_message or "desktop could not be started"},
                status=503,
            )
        viewer_id = None
        if payload.get("viewer_id"):
            try:
                registry.check(str(payload["viewer_id"]), owner=target.owner, profile_home=str(target.profile_home))
                viewer_id = str(payload["viewer_id"])
            except ViewerError:
                viewer_id = None  # a stale id on ensure is not an error: hand out a fresh one
        try:
            return web.json_response(await _observe_body(target, decision, viewer_id=viewer_id))
        except ViewerError as exc:
            return _viewer_error(exc)

    async def _lease_route(request, op: str):
        from tools.bot_desktop import lease as _lease

        # Handing control back must work even after the desktop was switched off: a human lease left
        # behind would keep the agent refused forever.
        early, target, payload, decision = await _prologue(request, require_enabled=op == "acquire")
        if early is not None:
            return early
        force = payload.get("force", False)
        if op == "release" and not isinstance(force, bool):
            return web.json_response({"error": "invalid_force", "message": "force must be a JSON boolean"},
                                     status=400)
        viewer_id = str(payload.get("viewer_id") or "")
        try:
            registry.check(viewer_id, owner=target.owner, profile_home=str(target.profile_home))
        except ViewerError as exc:
            return _viewer_error(exc)
        if op == "release":
            lease = await asyncio.to_thread(release_lease, target, viewer_id, force=force is True)
            return web.json_response({"lease": _lease.public_view(lease)})
        try:
            lease = await asyncio.to_thread(acquire_lease, target, viewer_id, decision)
        except DesktopGone as exc:
            return web.json_response({"error": "desktop_not_running", "message": str(exc)}, status=503)
        except ds.DesktopSandboxError as exc:
            return web.json_response({"error": exc.reason, "message": str(exc)}, status=503)
        return web.json_response({"lease": _lease.public_view(lease)})

    async def handle_acquire(request):
        return await _lease_route(request, "acquire")

    async def handle_release(request):
        return await _lease_route(request, "release")

    async def handle_ws(request):
        # Bearer failures are refused BEFORE the upgrade (the caller is the WebUI server, not a
        # browser). Everything about the viewer and the desktop is refused AFTER accept so the close
        # code reaches the browser through the WebUI relay: 4401 re-observe, 4403 not yours, 4001 gone.
        if not authorize(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        viewer_id = request.query.get("viewer_id", "")
        # Authorize and claim the viewer BEFORE the first await: once claimed it counts as attached,
        # so a concurrent observe can no longer evict it while this handshake is in flight.
        refusal: Optional[tuple[int, str]] = None
        target: Optional[Target] = None
        try:
            target = _target(request, {"viewer_id": viewer_id})
            registry.claim_socket(viewer_id, owner=target.owner, profile_home=str(target.profile_home))
        except PermissionError as exc:
            refusal = (CLOSE_NOT_ALLOWED, str(exc))
        except ViewerError as exc:
            refusal = (exc.ws_code, str(exc))
        ws = web.WebSocketResponse(max_msg_size=WS_MAX_MSG_SIZE, autoping=True, heartbeat=30.0)
        if refusal is not None:
            await ws.prepare(request)
            await ws.close(code=refusal[0], message=refusal[1][:100].encode())
            return ws
        outcome = {"clean": False}
        try:
            await ws.prepare(request)
            decision = await asyncio.to_thread(desktop_decision, target.profile_home)
            if not decision.enabled:
                await ws.close(code=CLOSE_NOT_ALLOWED, message=b"desktop_disabled")
                return ws
            await bridge(ws, target, viewer_id, decision.state_dir, registry=registry, outcome=outcome)
            return ws
        finally:
            # aiohttp cancels this handler once the peer's close completes, so the bridge may never
            # return: the clean-close verdict is read from ``outcome`` and the hand-back is shielded.
            if registry.release_socket(viewer_id, clean=outcome["clean"]):
                await asyncio.shield(asyncio.ensure_future(_hand_back(target, viewer_id)))

    async def _hand_back(target: Target, viewer_id: str) -> None:
        """The viewer's last socket closed cleanly (1000/1001): hand control back if it still holds it."""
        from tools.bot_desktop import lease as _lease

        try:
            if await asyncio.to_thread(_lease.viewer_may_send_input, viewer_id, profile_key=str(target.profile_home)):
                await asyncio.to_thread(release_lease, target, viewer_id)
        finally:
            registry.finish_release(viewer_id)

    app.router.add_post("/api/run-broker/desktop/observe", handle_observe)
    app.router.add_post("/api/run-broker/desktop/ensure", handle_ensure)
    app.router.add_post("/api/run-broker/desktop/lease/acquire", handle_acquire)
    app.router.add_post("/api/run-broker/desktop/lease/release", handle_release)
    app.router.add_get("/api/run-broker/desktop/ws", handle_ws)


async def bridge(ws, target: Target, viewer_id: str, state_dir: Path, *,
                 registry: ViewerRegistry = VIEWERS, outcome: Optional[dict] = None) -> None:
    """Pump RFB bytes between an accepted aiohttp WebSocket and THIS profile's Xvnc, gated by the lease.
    ``outcome["clean"]`` is set True the moment the viewer closes cleanly (1000/1001); the caller owns
    the lease decision. Closing the viewer window hands control back; a DROPPED link keeps the human's
    exclusion (they may be mid-login and the agent must not resume into that screen)."""
    outcome = outcome if outcome is not None else {}
    from aiohttp import WSMsgType
    from hermes_constants import hermes_home_key
    from tools.bot_desktop import lease as _lease
    from tools.bot_desktop.rfb_filter import RfbClientFilter

    from . import desktop_sandbox as ds

    home = str(target.profile_home)
    sock = target.profile_home / ds.RFB_SOCKET_RELPATH
    if not ds._socket_present(sock):
        await ws.close(code=CLOSE_DESKTOP_GONE, message=b"Bot Desktop is not running")
        return
    try:
        reader, writer = await asyncio.open_unix_connection(str(sock))
    except OSError as exc:
        logger.warning("[multitenancy] desktop viewer: cannot reach RFB socket %s: %s", sock, exc)
        await ws.close(code=CLOSE_DESKTOP_GONE, message=b"Bot Desktop socket unreachable")
        return

    loop = asyncio.get_running_loop()
    profile_key = hermes_home_key(home)
    evicted = asyncio.Event()
    desktop_gone = asyncio.Event()
    held = {"ever": _lease.viewer_may_send_input(viewer_id, profile_key=home)}
    allowed = {"input": held["ever"], "at": loop.time()}

    def _apply(lease) -> None:
        allowed["input"] = lease.holder == _lease.HUMAN and lease.viewer_id == viewer_id
        allowed["at"] = loop.time()
        if should_evict(held, lease, viewer_id):
            evicted.set()

    def _may_send_input() -> bool:
        if loop.time() - allowed["at"] > LEASE_REFRESH_S:
            _apply(_lease.get(profile_key=home))
        return allowed["input"]

    def _on_lease(key: str, lease) -> None:
        if key == profile_key:
            loop.call_soon_threadsafe(_apply, lease)

    unsubscribe = _lease.on_change(_on_lease)
    rfb_filter = RfbClientFilter(_may_send_input)

    async def rfb_to_ws() -> None:
        while True:
            chunk = await reader.read(READ_CHUNK)
            if not chunk:
                desktop_gone.set()
                return
            await ws.send_bytes(chunk)  # awaiting the send is the backpressure toward Xvnc

    async def ws_to_rfb() -> None:
        while True:
            msg = await ws.receive()
            if msg.type == WSMsgType.BINARY:
                try:
                    out = rfb_filter.feed(msg.data)
                except ValueError as exc:
                    await ws.close(code=CLOSE_PROTOCOL, message=str(exc)[:100].encode())
                    return
                if out:
                    writer.write(out)
                    await writer.drain()  # backpressure toward the browser
                continue
            if msg.type == WSMsgType.TEXT:
                await ws.close(code=CLOSE_PROTOCOL, message=b"RFB is binary")
                return
            # CLOSE carries the peer's code; CLOSED/CLOSING/ERROR = dropped link or our own close.
            if msg.type == WSMsgType.CLOSE and msg.data in CLEAN_CLOSE:
                outcome["clean"] = True
            return

    async def watch_lease() -> None:
        stamped = 0.0
        while True:
            await asyncio.sleep(LEASE_REFRESH_S)
            _apply(_lease.get(profile_key=home))
            if evicted.is_set():
                await ws.close(code=CLOSE_CONTROL_TAKEN, message=b"control-taken")
                return
            now = loop.time()
            if now - stamped >= ACTIVITY_STAMP_S:
                # An attached viewer is use: the idle sweep must not stop a screen someone watches.
                stamped = now
                registry.touch(viewer_id)
                try:
                    await asyncio.to_thread(ds.touch_last_used, state_dir)
                except OSError as exc:
                    logger.warning("[multitenancy] desktop viewer: touch_last_used failed: %s", exc)

    tasks = [asyncio.create_task(rfb_to_ws()), asyncio.create_task(ws_to_rfb()),
             asyncio.create_task(watch_lease())]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for t in done:
            exc = t.exception()
            if exc and not isinstance(exc, (ConnectionError, asyncio.CancelledError)):
                logger.debug("[multitenancy] desktop viewer ws ended: %r", exc)
    finally:
        unsubscribe()
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:  # Xvnc already gone (ECONNRESET / EPIPE on the FIN)
            pass
        if not ws.closed:
            if desktop_gone.is_set():
                await ws.close(code=CLOSE_DESKTOP_GONE, message=b"Bot Desktop closed")
            else:
                await ws.close()
