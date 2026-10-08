"""Per-profile podman desktop sandbox: decision, wrapper selection, fail-closed,
lifecycle, locks, reconciliation and toolset policy. Every podman call is
mocked — nothing here needs a container runtime."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_multitenancy import desktop_sandbox as ds


def _audit_events(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _completed(args: list[str], *, returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=stdout, stderr=stderr)


def _labels_from_run_args(args: list[str]) -> dict[str, str]:
    labels: dict[str, str] = {}
    for i, arg in enumerate(args[:-1]):
        if arg == "--label":
            key, _, value = args[i + 1].partition("=")
            labels[key] = value
    return labels


def _labels_for(decision: ds.DesktopDecision, *, image_id: str = "img-sha") -> dict[str, str]:
    """Labels a container created from ``decision`` carries (what inspect returns)."""
    return _labels_from_run_args(ds.run_args(decision, image_id=image_id))


class _FakePodman:
    """Scripted podman: records every argv, answers inspect/ps/run/start/exec/rm.

    ``state`` None means the container does not exist. ``labels`` are what
    ``inspect`` reports for an existing container; a ``run`` records the labels
    it was given so a later inspect matches the decision that created it.
    """

    def __init__(
        self,
        *,
        state: str | None = None,
        running: int = 0,
        screen_sock: Path | None = None,
        labels: dict[str, str] | None = None,
        image_id: str = "img-sha",
        screen_running: bool = True,
    ):
        self.calls: list[list[str]] = []
        self.state = state
        self.running = running
        self.screen_sock = screen_sock
        self.labels = dict(labels or {})
        self.image_id = image_id
        self.container_image_id = image_id
        self.screen_running = screen_running
        self.fail_run = False
        self.fail_screen_start = False
        self.fail_probe: int | None = None
        self.probe_count = 0
        self.inspect_error: str | None = None
        self.on_run = None
        self.info_returncode = 0
        self.info_stdout = "true\n"

    def __call__(self, podman_bin: str, args: list[str], *, timeout=None, check=False):
        self.calls.append([podman_bin, *args])
        verb = args[0]
        if verb == "info":
            return _completed(args, returncode=self.info_returncode, stdout=self.info_stdout,
                              stderr="" if self.info_returncode == 0 else "cannot set up namespace")
        if verb == "inspect":
            if self.inspect_error is not None:
                return _completed(args, returncode=125, stderr=self.inspect_error)
            if self.state is None:
                return _completed(args, returncode=125, stderr=f"Error: no such container {args[-1]}")
            row = {
                "State": {"Status": self.state},
                "Config": {"Labels": dict(self.labels)},
                "Image": self.container_image_id,
            }
            return _completed(args, stdout=json.dumps([row]))
        if verb == "image":
            return _completed(args, stdout=f"{self.image_id}\n")
        if verb == "ps":
            rows = [
                {
                    "Names": [f"hermes-p-other{i}"],
                    "Labels": {ds.LABEL_ROLE: ds.LABEL_ROLE_VALUE},
                }
                for i in range(self.running)
            ]
            return _completed(args, stdout=json.dumps(rows))
        if verb == "network":
            return _completed(args)
        if verb == "run":
            if self.on_run is not None:
                self.on_run()
            if self.fail_run:
                return _completed(args, returncode=126, stderr="image not found")
            self.state = "running"
            self.labels = _labels_from_run_args(args)
            self.container_image_id = self.image_id
            return _completed(args, stdout="abc123\n")
        if verb == "start":
            self.state = "running"
            return _completed(args)
        if verb == "rm":
            self.state = None
            self.labels = {}
            return _completed(args)
        if verb == "exec":
            if "computer-use" not in args:
                self.probe_count += 1
                return _completed(args, returncode=1 if self.fail_probe == self.probe_count else 0)
            if args[-1] == "--json":
                status = {"running": self.screen_running, "socket": str(self.screen_sock or "")}
                return _completed(args, stdout=json.dumps(status))
            if self.fail_screen_start:
                return _completed(args, returncode=1, stderr="Xvnc failed")
            if self.screen_sock is not None:
                _make_socket(self.screen_sock)
            return _completed(args, stdout="Bot Desktop [default]: running on DISPLAY :20\n")
        if verb == "stop":
            self.state = "exited"
            return _completed(args)
        raise AssertionError(f"unexpected podman verb {args!r}")


def _make_socket(path: Path) -> None:
    """A real unix socket inode at ``path`` (bound in a short dir: AF_UNIX paths are length-limited)."""
    import socket
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        path.unlink()
    short_dir = tempfile.mkdtemp(prefix="s", dir="/tmp")
    short = Path(short_dir) / "s"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(short))
    sock.close()
    os.replace(short, path)
    os.rmdir(short_dir)


@pytest.fixture(autouse=True)
def rootful_gateway(monkeypatch):
    """Every test runs as a root gateway (rootful podman) unless it says otherwise."""
    monkeypatch.setattr(ds, "_effective_uid", lambda: 0)


@pytest.fixture
def profile(tmp_path: Path) -> Path:
    home = tmp_path / ".hermes" / "profiles" / "desktop_smoke"
    (home / "workspace").mkdir(parents=True)
    return home.resolve()


@pytest.fixture
def podman_ok(monkeypatch, tmp_path: Path):
    fake_bin = tmp_path / "podman"
    fake_bin.write_text("#!/bin/sh\nexit 0\n")
    fake_bin.chmod(0o755)
    monkeypatch.setattr(ds, "_selinux_enforcing", lambda: False)
    monkeypatch.setattr(ds.time, "sleep", lambda _s: None)
    monkeypatch.delenv("HERMES_SHARED_HOME", raising=False)
    return str(fake_bin)


DESKTOP_ON = {"multitenancy": {"desktop": {"enabled": True}}}


def _shared_on(podman_bin: str, **extra) -> dict:
    return {"multitenancy": {"desktop": {"enabled": True, "podman_bin": podman_bin, **extra}}}


def _decision(profile: Path, podman_bin: str, **extra) -> ds.DesktopDecision:
    return ds.desktop_decision(_shared_on(podman_bin, **extra), profile)


# --- decision / config ---------------------------------------------------------


def test_desktop_decision_defaults_off(profile: Path):
    decision = ds.desktop_decision({}, profile)
    assert decision.enabled is False
    assert "disabled" in decision.reason


def test_desktop_decision_profile_override_enables_with_defaults(profile: Path):
    decision = ds.desktop_decision({}, profile, profile_config=DESKTOP_ON)
    assert decision.enabled is True
    assert decision.container_name == ds.container_name(profile)
    assert decision.image == ds.DEFAULT_IMAGE
    assert decision.podman_bin == "podman"
    assert decision.idle_stop_minutes == 30
    assert decision.max_containers_per_host == 60
    assert decision.ensure_timeout_s == 30.0 and decision.screen_timeout_s == 20.0
    assert decision.rfb_socket == profile / "bot-desktop" / "rfb.sock"
    assert decision.exec_env == {"XDG_RUNTIME_DIR": ds.CONTAINER_RUNTIME_DIR}
    assert decision.shared_home == profile.parent.parent


def test_desktop_decision_reads_every_host_knob_from_shared_config(profile: Path):
    decision = ds.desktop_decision(
        {
            "multitenancy": {
                "desktop": {
                    "enabled": "true",
                    "image": "registry.example/hermes/desktop:0.21.5-20261008",
                    "podman_bin": "/usr/local/bin/podman",
                    "idle_stop_minutes": "1",
                    "max_containers_per_host": 2,
                    "exec_env": {"HERMES_CUA_DRIVER_CMD": "/x/cua-driver", "PATH": "/evil"},
                    "memory": "4g",
                }
            }
        },
        profile,
    )
    assert decision.image == "registry.example/hermes/desktop:0.21.5-20261008"
    assert decision.podman_bin == "/usr/local/bin/podman"
    assert decision.idle_stop_minutes == 1
    assert decision.max_containers_per_host == 2
    assert decision.exec_env["HERMES_CUA_DRIVER_CMD"] == "/x/cua-driver"
    # PATH can never be overridden through exec_env: it is rebuilt for the image.
    assert "PATH" not in decision.exec_env
    assert decision.memory == "4g"


@pytest.mark.parametrize("key,value", sorted({
    "podman_bin": "/tmp/evil-podman",
    "network": "host",
    "image": "evil/image:latest",
    "subid_base": 0,
    "max_containers_per_host": 9999,
    "memory": "64g",
    "cpus": "32",
    "shm_size": "8g",
    "exec_env": {"LD_PRELOAD": "/x.so"},
    "container_python": "/bin/sh",
    "container_hermes": "/bin/sh",
    "container_path": "/evil",
    "ensure_timeout_s": 1,
    "screen_timeout_s": 1,
    "rootless": "false",
}.items()))
def test_profile_config_host_only_keys_are_ignored_with_warning(profile: Path, caplog, key, value):
    """P0: a tenant-writable profile config must never control the host runtime."""
    assert key in ds.HOST_ONLY_KEYS
    defaults = ds.desktop_decision(DESKTOP_ON, profile)
    with caplog.at_level("WARNING", logger="hermes_multitenancy.desktop_sandbox"):
        decision = ds.desktop_decision(
            DESKTOP_ON, profile, profile_config={"multitenancy": {"desktop": {"enabled": True, key: value}}},
        )
    assert decision.enabled is True
    assert decision.ignored_profile_keys == (key,)
    assert getattr(decision, key) == getattr(defaults, key)
    assert any("host-only keys" in rec.message and key in rec.message for rec in caplog.records)


def test_host_only_keys_cover_every_runtime_field():
    runtime_fields = {
        f for f in ds.DesktopDecision.__dataclass_fields__
        if f not in {"enabled", "reason", "profile_home", "profile_name", "shared_home",
                     "idle_stop_minutes", "ignored_profile_keys"}
    }
    assert runtime_fields == set(ds.HOST_ONLY_KEYS)


@pytest.mark.parametrize(
    ("shared_idle", "profile_idle", "expected"),
    [(30, 5, 5), (30, 30, 30), (30, 31, 30), (30, 0, 30), (30, "abc", 30), (0, 7, 7), (0, 0, 0)],
)
def test_profile_may_only_shorten_idle_window(profile: Path, shared_idle, profile_idle, expected):
    decision = ds.desktop_decision(
        {"multitenancy": {"desktop": {"enabled": True, "idle_stop_minutes": shared_idle}}},
        profile,
        profile_config={"multitenancy": {"desktop": {"idle_stop_minutes": profile_idle}}},
    )
    assert decision.idle_stop_minutes == expected


def test_desktop_decision_router_profile_never_enabled(tmp_path: Path):
    router = tmp_path / "profiles" / "multitenancy_router"
    router.mkdir(parents=True)
    assert ds.desktop_decision(DESKTOP_ON, router).enabled is False
    assert ds.desktop_decision({}, router, profile_config=DESKTOP_ON).enabled is False


def test_shared_config_default_off_profile_config_can_enable_but_not_choose_image(tmp_path: Path, monkeypatch):
    """The gateway reads both files separately; the profile flips it on, the shared file picks the image."""
    from hermes_multitenancy.agent_real import _core

    shared = tmp_path / ".hermes"
    home = shared / "profiles" / "alice"
    home.mkdir(parents=True)
    (shared / "config.yaml").write_text("multitenancy:\n  desktop:\n    enabled: false\n    image: img:shared\n")
    monkeypatch.delenv("HERMES_SHARED_HOME", raising=False)
    assert _core._desktop_decision_for_profile(home).enabled is False

    (home / "config.yaml").write_text(
        "multitenancy:\n  desktop:\n    enabled: true\n    image: img:tenant\n    podman_bin: /tmp/evil\n"
    )
    decision = _core._desktop_decision_for_profile(home)
    assert decision.enabled is True
    assert decision.image == "img:shared"
    assert decision.podman_bin == "podman"
    assert decision.ignored_profile_keys == ("image", "podman_bin")


def test_container_name_is_hash_of_canonical_profile_path(tmp_path: Path):
    a = tmp_path / "profiles" / "odd name/with:chars"
    b = tmp_path / "profiles" / "odd-name-with-chars"
    digest = hashlib.sha256(str(a.resolve()).encode()).hexdigest()[:12]
    assert ds.container_name(a) == f"hermes-p-{digest}"
    assert ds.container_name(a) != ds.container_name(b)
    assert len(ds.container_name(a)) == len("hermes-p-") + 12


def test_host_desktop_settings_reads_shared_config_only(tmp_path: Path):
    (tmp_path / "config.yaml").write_text("multitenancy:\n  desktop:\n    podman_bin: /opt/podman/bin/podman\n")
    assert ds.host_desktop_settings(tmp_path).podman_bin == "/opt/podman/bin/podman"
    assert ds.host_desktop_settings(tmp_path / "missing").podman_bin == "podman"


# --- gateway-owned state --------------------------------------------------------------


def test_state_dir_lives_under_shared_home_not_profile(profile: Path):
    state = ds.profile_state_dir(profile.parent.parent, profile)
    assert state.parent == profile.parent.parent / "desktop-state"
    assert state.name == hashlib.sha256(str(profile).encode()).hexdigest()[:16]
    assert profile not in state.parents


def test_state_dir_refuses_to_live_inside_profile(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("HERMES_SHARED_HOME", raising=False)
    lone = tmp_path / "lone-profile"
    lone.mkdir()
    with pytest.raises(ds.DesktopSandboxError) as excinfo:
        ds.profile_state_dir(lone, lone)
    assert excinfo.value.reason == "desktop_state_dir_unavailable"


def test_turn_leases_count_only_live_entries(tmp_path: Path):
    state = tmp_path / "state"
    now = 5_000_000.0
    token = ds.begin_turn(state, now=now)
    assert ds.active_turn_count(state, now=now) == 1
    # Leaked lease: dead pid or too old → not counted.
    turns = json.loads((state / "turns.json").read_text())
    turns["dead"] = {"pid": 2**22 - 1, "started": now}
    turns["old"] = {"pid": os.getpid(), "started": now - ds.ACTIVE_TURN_MAX_S - 1}
    (state / "turns.json").write_text(json.dumps(turns))
    assert ds.active_turn_count(state, now=now) == 1
    ds.end_turn(state, token, now=now + 10)
    assert ds.active_turn_count(state, now=now + 10) == 0
    assert ds.read_last_used(state) == now + 10
    assert oct((state / "turns.json").stat().st_mode & 0o777) == "0o600"


# --- browser + toolsets ----------------------------------------------------------


def test_browser_decision_desktop_implies_browser_but_router_still_denied(tmp_path: Path):
    from hermes_multitenancy.browser_policy import browser_decision

    alice = tmp_path / "profiles" / "alice"
    alice.mkdir(parents=True)
    decision = browser_decision(DESKTOP_ON, alice)
    assert decision.enabled is True
    assert decision.backend == "local"

    router = tmp_path / "profiles" / "multitenancy_router"
    router.mkdir(parents=True)
    assert browser_decision(DESKTOP_ON, router).enabled is False
    assert browser_decision({}, alice).enabled is False


def test_resolve_enabled_toolsets_adds_computer_use_only_for_desktop_profiles(tmp_path: Path):
    from hermes_multitenancy import agent_real

    profile_home = tmp_path / "profiles" / "alice"
    profile_home.mkdir(parents=True)

    def fake_get_platform_tools(config, platform, *, include_default_mcp_servers=True):
        # api_server/webui core default strips computer_use.
        return {"web", "file", "browser"}

    off = agent_real._resolve_enabled_toolsets(
        {}, "webui", platform_tools_resolver=fake_get_platform_tools, profile_home=profile_home,
    )
    assert "computer_use" not in off
    assert "browser" not in off

    on = agent_real._resolve_enabled_toolsets(
        DESKTOP_ON, "webui", platform_tools_resolver=fake_get_platform_tools, profile_home=profile_home,
    )
    assert on == ["browser", "computer_use", "file", "request-authorization", "web"]


def test_desktop_toolsets_for_policy_leaves_none_alone():
    assert ds.desktop_toolsets_for_policy(None, True) is None
    assert ds.desktop_toolsets_for_policy(["web"], True) == ["browser", "computer_use", "web"]
    assert ds.desktop_toolsets_for_policy(["web"], False) == ["web"]


# --- wrapper selection ----------------------------------------------------------------


def _linux(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import agent_real

    fake_policy = tmp_path / "bwrap.args"
    fake_policy.write_text("--die-with-parent\n--bind ${PROFILE_HOME} ${PROFILE_HOME}\n")
    fake_bin = tmp_path / "bwrap"
    fake_bin.write_text("#!/bin/sh\nexec \"$@\"\n")
    fake_bin.chmod(0o755)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("HERMES_USE_SANDBOX", "1")
    monkeypatch.delenv("HERMES_SANDBOX_PROFILES", raising=False)
    monkeypatch.delenv("HERMES_SHARED_HOME", raising=False)
    monkeypatch.setenv("HERMES_AGENT_REPO", str(tmp_path / "agent-repo"))
    monkeypatch.setattr(agent_real, "_BWRAP_ARGS_FILE", fake_policy)
    monkeypatch.setattr(agent_real, "_BWRAP_EXEC", str(fake_bin))
    monkeypatch.setattr(ds, "_selinux_enforcing", lambda: False)
    monkeypatch.setattr(ds.time, "sleep", lambda _s: None)
    return agent_real


def _desktop_profile(tmp_path: Path, podman_bin: str, *, profile_yaml: str = "multitenancy:\n  desktop:\n    enabled: true\n") -> Path:
    """A shared home whose config names the podman binary + one desktop profile."""
    shared = tmp_path / ".hermes"
    profile = shared / "profiles" / "desktop_smoke"
    (profile / "workspace").mkdir(parents=True)
    (shared / "config.yaml").write_text(f"multitenancy:\n  desktop:\n    podman_bin: {podman_bin}\n")
    (profile / "config.yaml").write_text(profile_yaml)
    return profile.resolve()


def _audit(monkeypatch, tmp_path: Path) -> Path:
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_PATH", str(audit))
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_ENABLED", "1")
    return audit


def test_wrap_without_desktop_flag_is_byte_identical_to_bwrap(monkeypatch, tmp_path: Path):
    """Regression guard: a non-desktop profile's argv is exactly the bwrap argv."""
    agent_real = _linux(monkeypatch, tmp_path)
    profile = tmp_path / ".hermes" / "profiles" / "alice"
    profile.mkdir(parents=True)
    cmd = ["/usr/bin/python3", "child.py"]

    expected = agent_real._wrap_linux_bwrap(cmd, profile)
    assert agent_real._wrap_with_sandbox(cmd, profile) == expected
    assert agent_real._wrap_with_sandbox(cmd, profile, env={"X": "1"}) == expected
    assert expected[0].endswith("bwrap")
    assert not any("podman" in part for part in expected)


def test_wrap_selects_container_backend_for_desktop_profile(monkeypatch, tmp_path: Path, podman_ok):
    agent_real = _linux(monkeypatch, tmp_path)
    profile = _desktop_profile(tmp_path, podman_ok)
    (tmp_path / ".hermes" / "config.yaml").write_text(
        f"multitenancy:\n  desktop:\n    podman_bin: {podman_ok}\n    exec_env:\n      CUSTOM_CONTAINER_ENV: configured\n"
    )
    decision = agent_real._desktop_decision_for_profile(profile)
    fake = _FakePodman(state="running", screen_sock=decision.rfb_socket, labels=_labels_for(decision))
    _make_socket(decision.rfb_socket)
    monkeypatch.setattr(ds, "_run_podman", fake)

    env = {
        "OPENAI_API_KEY": "sk-secret-value",
        "HERMES_HOME": str(profile),
        "PATH": "/host/venv/bin:/usr/bin",
        "SSL_CERT_FILE": "/etc/pki/tls/cert.pem",
        "HERMES_CUA_DRIVER_CMD": "/host/cua-driver",
        "AGENT_BROWSER_ARGS": "--host-browser-args",
        "AGENT_BROWSER_EXECUTABLE_PATH": "/host/chromium",
    }
    wrapped = agent_real._wrap_with_sandbox([sys.executable, "/mt/hermes_multitenancy/aiagent_subprocess.py"], profile, env=env)

    assert wrapped[:7] == [podman_ok, "exec", "-i", "--user", "10000:10000", "-w", str(profile / "workspace")]
    name_idx = wrapped.index(decision.container_name)
    assert wrapped[name_idx + 1:] == [ds.DEFAULT_CONTAINER_PYTHON, "/mt/hermes_multitenancy/aiagent_subprocess.py"]
    # Secrets are forwarded by NAME only; values never enter argv.
    assert "sk-secret-value" not in " ".join(wrapped)
    assert ["-e", "OPENAI_API_KEY"] == wrapped[wrapped.index("OPENAI_API_KEY") - 1: wrapped.index("OPENAI_API_KEY") + 1]
    assert ["-e", "HERMES_HOME"] == wrapped[wrapped.index("HERMES_HOME") - 1: wrapped.index("HERMES_HOME") + 1]
    # Host PATH and host CA bundle paths are not forwarded; PATH is the image's.
    assert "PATH" not in wrapped
    assert "SSL_CERT_FILE" not in wrapped
    assert f"PATH={ds.DEFAULT_CONTAINER_PATH}" in wrapped
    assert f"XDG_RUNTIME_DIR={ds.CONTAINER_RUNTIME_DIR}" in wrapped
    assert "CUSTOM_CONTAINER_ENV=configured" in wrapped
    for image_env in (
        "HERMES_CUA_DRIVER_CMD",
        "AGENT_BROWSER_ARGS",
        "AGENT_BROWSER_EXECUTABLE_PATH",
    ):
        assert image_env not in wrapped
        assert not any(arg.startswith(f"{image_env}=") for arg in wrapped)
    # last_used stamped in the gateway-owned state dir, never under the profile.
    assert ds.read_last_used(decision.state_dir) is not None
    assert not (profile / "desktop").exists()
    # Container was already running with a live screen: reused, not restarted;
    # the only exec was the screen status probe.
    verbs = [call[1] for call in fake.calls]
    assert "run" not in verbs and "start" not in verbs
    assert [c for c in fake.calls if c[1] == "exec"][0][-2:] == ["status", "--json"]


def test_wrap_container_requires_env(monkeypatch, tmp_path: Path, podman_ok):
    agent_real = _linux(monkeypatch, tmp_path)
    audit = _audit(monkeypatch, tmp_path)
    profile = _desktop_profile(tmp_path, podman_ok)

    with pytest.raises(RuntimeError, match="desktop_exec_env_missing"):
        agent_real._wrap_with_sandbox([sys.executable, "child.py"], profile)
    assert _audit_events(audit)[-1]["reason"] == "desktop_exec_env_missing"


def test_wrap_container_fails_closed_when_podman_missing(monkeypatch, tmp_path: Path):
    """Acceptance 5: podman gone → refuse to spawn, security event, no bare run."""
    agent_real = _linux(monkeypatch, tmp_path)
    audit = _audit(monkeypatch, tmp_path)
    profile = _desktop_profile(tmp_path, str(tmp_path / "secret-token-nope"))

    with pytest.raises(RuntimeError, match="desktop_container_unavailable"):
        agent_real._wrap_with_sandbox([sys.executable, "child.py"], profile, env={"A": "b"})

    events = _audit_events(audit)
    assert events[-1]["event_type"] == "sandbox.denied"
    assert events[-1]["reason"] == "desktop_container_unavailable"
    assert "secret-token" not in audit.read_text()


def test_profile_podman_bin_cannot_redirect_the_gateway(monkeypatch, tmp_path: Path, podman_ok):
    """P0 negative: a profile pointing podman_bin at its own executable is ignored."""
    agent_real = _linux(monkeypatch, tmp_path)
    evil = tmp_path / "evil-podman"
    evil.write_text("#!/bin/sh\necho pwned\n")
    evil.chmod(0o755)
    profile = _desktop_profile(
        tmp_path, podman_ok,
        profile_yaml=f"multitenancy:\n  desktop:\n    enabled: true\n    podman_bin: {evil}\n    network: host\n",
    )
    decision = agent_real._desktop_decision_for_profile(profile)
    fake = _FakePodman(state="running", screen_sock=decision.rfb_socket, labels=_labels_for(decision))
    _make_socket(decision.rfb_socket)
    monkeypatch.setattr(ds, "_run_podman", fake)

    wrapped = agent_real._wrap_with_sandbox([sys.executable, "child.py"], profile, env={"A": "b"})
    assert wrapped[0] == podman_ok
    assert all(call[0] == podman_ok for call in fake.calls)
    assert str(evil) not in " ".join(wrapped)
    assert decision.network == ds.DEFAULT_NETWORK


def test_wrap_container_fails_closed_when_run_fails(monkeypatch, tmp_path: Path, podman_ok):
    agent_real = _linux(monkeypatch, tmp_path)
    audit = _audit(monkeypatch, tmp_path)
    profile = _desktop_profile(tmp_path, podman_ok)
    fake = _FakePodman(state=None)
    fake.fail_run = True
    monkeypatch.setattr(ds, "_run_podman", fake)

    with pytest.raises(RuntimeError, match="desktop_container_unavailable"):
        agent_real._wrap_with_sandbox([sys.executable, "child.py"], profile, env={"A": "b"})
    assert _audit_events(audit)[-1]["reason"] == "desktop_container_unavailable"


def test_wrap_container_rejects_unmappable_executable(monkeypatch, tmp_path: Path, podman_ok):
    agent_real = _linux(monkeypatch, tmp_path)
    audit = _audit(monkeypatch, tmp_path)
    profile = _desktop_profile(tmp_path, podman_ok)

    with pytest.raises(RuntimeError, match="desktop_exec_path_unmapped"):
        agent_real._wrap_with_sandbox(["/bin/cat", "x"], profile, env={"A": "b"})
    assert _audit_events(audit)[-1]["reason"] == "desktop_exec_path_unmapped"


@pytest.mark.parametrize("rel", [".env", "auth.json", "bot-desktop", "workspace/credentials", "tokens"])
def test_wrap_container_refuses_symlinked_profile_paths(monkeypatch, tmp_path: Path, podman_ok, rel):
    """P0 negative: a tenant-planted symlink on any gateway-touched path fails closed + audit."""
    agent_real = _linux(monkeypatch, tmp_path)
    audit = _audit(monkeypatch, tmp_path)
    profile = _desktop_profile(tmp_path, podman_ok)
    victim = tmp_path / "victim"
    victim.write_text("gateway-owned\n")
    target = profile / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(victim)
    fake = _FakePodman(state=None)
    monkeypatch.setattr(ds, "_run_podman", fake)

    with pytest.raises(RuntimeError, match="desktop_profile_home_tampered"):
        agent_real._wrap_with_sandbox([sys.executable, "child.py"], profile, env={"A": "b"})
    assert _audit_events(audit)[-1] == {
        **{k: v for k, v in _audit_events(audit)[-1].items() if k == "@timestamp"},
        "event_type": "sandbox.denied", "reason": "desktop_profile_home_tampered", "profile": "desktop_smoke",
    }
    assert "run" not in [c[1] for c in fake.calls]
    assert victim.read_text() == "gateway-owned\n"


def test_start_branch_also_refuses_symlinks(monkeypatch, tmp_path: Path, podman_ok, profile: Path):
    decision = _decision(profile, podman_ok)
    (profile / ".env").symlink_to(tmp_path / "elsewhere")
    fake = _FakePodman(state="exited", labels=_labels_for(decision))
    monkeypatch.setattr(ds, "_run_podman", fake)
    with pytest.raises(ds.DesktopSandboxError) as excinfo:
        ds.ensure(decision)
    assert excinfo.value.reason == "desktop_profile_home_tampered"
    assert "start" not in [c[1] for c in fake.calls]


def test_desktop_profile_with_sandbox_toggle_off_still_uses_container(monkeypatch, tmp_path: Path, podman_ok):
    """A desktop profile never runs bare on the gateway host."""
    agent_real = _linux(monkeypatch, tmp_path)
    monkeypatch.delenv("HERMES_USE_SANDBOX", raising=False)
    profile = _desktop_profile(tmp_path, podman_ok)
    decision = agent_real._desktop_decision_for_profile(profile)
    fake = _FakePodman(state="running", screen_sock=decision.rfb_socket, labels=_labels_for(decision))
    _make_socket(decision.rfb_socket)
    monkeypatch.setattr(ds, "_run_podman", fake)

    wrapped = agent_real._wrap_with_sandbox([sys.executable, "child.py"], profile, env={"A": "b"})
    assert wrapped[1] == "exec"


def test_darwin_ignores_desktop_flag(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import agent_real

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.delenv("HERMES_USE_SANDBOX", raising=False)
    profile = tmp_path / ".hermes" / "profiles" / "desktop_smoke"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("multitenancy:\n  desktop:\n    enabled: true\n")
    cmd = ["/usr/bin/python3", "child.py"]
    assert agent_real._wrap_with_sandbox(cmd, profile, env={}) == cmd


# --- callers: off the event loop + turn leases ---------------------------------------------


def test_every_async_caller_wraps_off_the_event_loop_and_holds_a_turn_lease():
    base = Path(__file__).resolve().parent.parent / "hermes_multitenancy" / "agent_real"
    core = (base / "_core.py").read_text(encoding="utf-8")
    streaming = (base / "streaming.py").read_text(encoding="utf-8")
    warm = (base / "warm_worker.py").read_text(encoding="utf-8")
    for source, name in ((core, "_core"), (streaming, "streaming"), (warm, "warm_worker")):
        assert "cmd, spawn_env = await asyncio.to_thread(\n        _sandbox_spawn" in source or (
            "cmd, spawn_env = await asyncio.to_thread(\n            _sandbox_spawn" in source
        ), name
        # The wrapped command is spawned with the spawn env, never the worker env.
        spawn = source[source.index("create_subprocess_exec(", source.index("_sandbox_spawn,")):]
        spawn = spawn[: spawn.index(")")]
        assert "env=spawn_env," in spawn and "env=env," not in spawn, name
    # No production spawn goes through the argv-only wrapper.
    for source in (streaming, warm):
        assert "_wrap_with_sandbox" not in source
    assert core.count("_wrap_with_sandbox(") == 2  # definition + _sandbox_spawn's non-desktop branch
    assert "_desktop_turn_scope(profile_home)" in core
    assert "_pkg._desktop_turn_scope(profile_home)" in streaming


def test_desktop_turn_scope_counts_only_desktop_profiles(monkeypatch, tmp_path: Path, podman_ok):
    agent_real = _linux(monkeypatch, tmp_path)
    profile = _desktop_profile(tmp_path, podman_ok)
    decision = agent_real._desktop_decision_for_profile(profile)
    with agent_real._desktop_turn_scope(profile):
        assert ds.active_turn_count(decision.state_dir) == 1
        with agent_real._desktop_turn_scope(profile):
            assert ds.active_turn_count(decision.state_dir) == 2
        assert ds.active_turn_count(decision.state_dir) == 1
    assert ds.active_turn_count(decision.state_dir) == 0
    assert ds.read_last_used(decision.state_dir) is not None

    plain = tmp_path / ".hermes" / "profiles" / "plain"
    plain.mkdir(parents=True)
    with agent_real._desktop_turn_scope(plain):
        pass
    assert not (tmp_path / ".hermes" / "desktop-state" / hashlib.sha256(str(plain.resolve()).encode()).hexdigest()[:16]).exists()


# --- lifecycle: ensure / run args / quota / screen -----------------------------------


def test_ensure_creates_container_with_hardened_run_args_and_starts_screen(monkeypatch, tmp_path: Path, podman_ok):
    profile = (tmp_path / ".hermes" / "profiles" / "desktop_smoke").resolve()
    (profile / "workspace").mkdir(parents=True)
    (profile / ".env").write_text("OPENAI_API_KEY=sk-secret\n")
    shared = tmp_path / ".hermes"
    (shared / "config.yaml").write_text("model: {}\n")
    (shared / ".env").write_text("SHARED=1\n")
    (shared / "bin").mkdir()
    (shared / "skills").mkdir()
    (shared / "desktop-state").mkdir()
    monkeypatch.setattr(ds, "_selinux_enforcing", lambda: True)
    decision = _decision(profile, podman_ok, image="img:test", idle_stop_minutes=7)
    fake = _FakePodman(state=None, screen_sock=decision.rfb_socket, image_id="sha256:img-test")
    monkeypatch.setattr(ds, "_run_podman", fake)

    handle = ds.ensure(decision)

    assert handle.started is True and handle.screen_started is True and handle.podman_bin == podman_ok
    verbs = [call[1] for call in fake.calls]
    assert verbs.index("network") < verbs.index("run") < verbs.index("exec")
    run = next(call for call in fake.calls if call[1] == "run")
    joined = " ".join(run)
    profile_resolved = str(profile)
    owner = profile.stat()
    assert f"--name {decision.container_name}" in joined
    assert "--replace" not in run
    assert "--init --entrypoint /bin/sleep --user 10000:10000" in joined
    assert f"--uidmap 0:{ds.DEFAULT_SUBID_BASE}:10000 --uidmap 10000:{owner.st_uid}:1" in joined
    assert f"--gidmap 10000:{owner.st_gid}:1" in joined
    assert "--cap-drop ALL" in joined and "--security-opt no-new-privileges" in joined
    assert "--memory 3g --cpus 2 --shm-size 1g --network hermes-desktop" in joined
    assert f"--label {ds.LABEL_PROFILE}=desktop_smoke" in joined
    assert f"--label {ds.LABEL_PROFILE_HOME}={profile_resolved}" in joined
    assert f"--label {ds.LABEL_SPEC}={decision.spec_hash(owner.st_uid, owner.st_gid)}" in joined
    assert f"--label {ds.LABEL_IMAGE_ID}=sha256:img-test" in joined
    cfg = (shared / "config.yaml").stat()
    assert f"--label {ds.LABEL_SHARED_CONFIG}={cfg.st_dev}:{cfg.st_ino}" in joined
    assert ds.LABEL_IDLE_STOP_MINUTES not in joined if hasattr(ds, "LABEL_IDLE_STOP_MINUTES") else True
    # Same-path mounts: profile rw (Z), MT repo ro (z), /workspace compat.
    assert f"-v {profile_resolved}:{profile_resolved}:rw,Z" in joined
    assert f"-v {profile_resolved}/workspace:/workspace:rw,Z" in joined
    mt_repo = str(Path(ds.__file__).resolve().parent.parent)
    assert f"-v {mt_repo}:{mt_repo}:ro,z" in joined
    # Masks: .env → /dev/null, auth.json → empty store, four tmpfs dirs, runtime dir.
    assert f"type=bind,src=/dev/null,dst={profile_resolved}/.env,ro" in joined
    assert f"sandbox/empty-auth.json,dst={profile_resolved}/auth.json,ro" in joined
    for rel in ds.TMPFS_PROFILE_SUBDIRS:
        assert f"--tmpfs {profile_resolved}/{rel}" in joined
    assert f"--tmpfs {ds.CONTAINER_RUNTIME_DIR}" in joined
    # Shared home: allowlist only, secrets masked, sibling profiles and lifecycle state absent.
    shared_resolved = str(shared.resolve())
    assert f"type=bind,src={shared_resolved}/config.yaml,dst={shared_resolved}/config.yaml,ro,relabel=shared" in joined
    assert f"type=bind,src=/dev/null,dst={shared_resolved}/.env,ro" in joined
    assert f"-v {shared_resolved}/bin:{shared_resolved}/bin:ro,z" in joined
    assert f"-v {shared_resolved}/skills:{shared_resolved}/skills:ro,z" in joined
    assert f"{shared_resolved}/profiles:" not in joined
    assert "desktop-state" not in joined
    assert "sk-secret" not in joined
    assert run[-2:] == ["img:test", "infinity"]
    # Mask targets pre-created by the gateway (never by podman as root), 0700/0600.
    assert (profile / "auth.json").read_text().strip() == "{}"
    assert oct((profile / "auth.json").stat().st_mode & 0o777) == "0o600"
    for rel in ds.TMPFS_PROFILE_SUBDIRS:
        assert (profile / rel).is_dir()
    assert not (profile / "desktop").exists()
    # After a (re)start: reachability probe first, then screen start as 10000
    # with HERMES_HOME pointing at the profile.
    execs = [call for call in fake.calls if call[1] == "exec"]
    assert execs[0][-3:] == ["test", "-r", f"{shared_resolved}/config.yaml"]
    assert f"mkdir -p '{profile_resolved}/bot-desktop'" in execs[1][-1]
    assert f"touch '{profile_resolved}/bot-desktop/.desktop-sandbox-write-probe'" in execs[1][-1]
    assert execs[2][-3:] == ["test", "-r", f"{profile_resolved}/config.yaml"]
    screen = execs[3]
    assert "--user" in screen and f"HERMES_HOME={profile_resolved}" in screen
    assert screen[-4:] == [ds.DEFAULT_CONTAINER_HERMES, "computer-use", "screen", "start"]
    # Gateway-owned state written: last_used + meta with the idle window.
    assert ds.read_last_used(decision.state_dir) is not None
    meta = json.loads((decision.state_dir / "meta.json").read_text())
    assert meta["idle_stop_minutes"] == 7 and meta["container"] == decision.container_name
    assert oct(decision.state_dir.stat().st_mode & 0o777) == "0o700"


@pytest.mark.parametrize(
    ("failed_probe", "path_fragment"),
    [(1, "shared config"), (2, "bot-desktop"), (3, "profile config")],
)
def test_ensure_reports_each_unreachable_profile_path_precisely_and_rolls_back(
    monkeypatch, tmp_path: Path, podman_ok, profile: Path, failed_probe: int, path_fragment: str,
):
    (profile.parent.parent / "config.yaml").write_text("model: {}\n")
    (profile / "config.yaml").write_text("multitenancy: {}\n")
    decision = _decision(profile, podman_ok)
    fake = _FakePodman(state=None, screen_sock=decision.rfb_socket)
    fake.fail_probe = failed_probe
    monkeypatch.setattr(ds, "_run_podman", fake)

    with pytest.raises(ds.DesktopSandboxError) as excinfo:
        ds.ensure(decision)
    assert excinfo.value.reason == "desktop_profile_home_unreachable"
    assert path_fragment in str(excinfo.value)
    assert "SELinux" in str(excinfo.value)
    assert not any(call[1] == "exec" and "start" == call[-1] for call in fake.calls)
    # The container this call created is not leaked: stopped and removed.
    verbs = [call[1] for call in fake.calls]
    assert verbs[-2:] == ["stop", "rm"]
    assert fake.state is None


@pytest.mark.parametrize("selinux", [True, False])
def test_file_bind_mounts_use_shared_relabel_only_under_selinux(monkeypatch, tmp_path: Path, profile: Path, selinux: bool):
    shared = profile.parent.parent
    (shared / "config.yaml").write_text("model: {}\n")
    (shared / "active_profile").write_text("desktop_smoke\n")
    (shared / ".env").write_text("SECRET=hidden\n")
    (shared / "auth.lock").touch()
    (shared / "auth.json").write_text("{}\n")
    monkeypatch.setattr(ds, "_selinux_enforcing", lambda: selinux)

    args = ds.run_args(ds.desktop_decision(DESKTOP_ON, profile))
    mounts = [args[i + 1] for i, arg in enumerate(args[:-1]) if arg == "--mount"]

    assert mounts
    for mount in mounts:
        if "src=/dev/null," in mount:
            assert "relabel=" not in mount
        else:
            assert ("relabel=shared" in mount) is selinux


def test_ensure_restarts_exited_container_and_restarts_screen(monkeypatch, tmp_path: Path, podman_ok, profile: Path):
    decision = _decision(profile, podman_ok)
    decision.rfb_socket.parent.mkdir(parents=True)
    decision.rfb_socket.touch()  # stale regular file from the previous life
    fake = _FakePodman(state="exited", screen_sock=decision.rfb_socket, labels=_labels_for(decision))
    monkeypatch.setattr(ds, "_run_podman", fake)

    handle = ds.ensure(decision)

    assert handle.started is True and handle.screen_started is True
    verbs = [call[1] for call in fake.calls]
    assert "start" in verbs and "run" not in verbs and "exec" in verbs
    # Quota is checked on the stopped→running transition too.
    assert verbs.index("ps") < verbs.index("start")


def test_ensure_running_container_with_dead_screen_restarts_screen_only(monkeypatch, tmp_path: Path, podman_ok, profile: Path):
    decision = _decision(profile, podman_ok)
    _make_socket(decision.rfb_socket)  # socket exists but screen status says not running
    fake = _FakePodman(state="running", screen_sock=decision.rfb_socket, labels=_labels_for(decision), screen_running=False)
    monkeypatch.setattr(ds, "_run_podman", fake)

    handle = ds.ensure(decision)

    assert handle.started is False and handle.screen_started is True
    assert [call[1] for call in fake.calls if call[1] in {"run", "start"}] == []
    execs = [c for c in fake.calls if c[1] == "exec"]
    assert execs[0][-2:] == ["status", "--json"]
    assert execs[-1][-1] == "start"


def test_ensure_running_container_with_stale_socket_file_restarts_screen(monkeypatch, tmp_path: Path, podman_ok, profile: Path):
    decision = _decision(profile, podman_ok)
    decision.rfb_socket.parent.mkdir(parents=True)
    decision.rfb_socket.write_text("not a socket")
    fake = _FakePodman(state="running", screen_sock=decision.rfb_socket, labels=_labels_for(decision))
    monkeypatch.setattr(ds, "_run_podman", fake)

    handle = ds.ensure(decision)
    assert handle.screen_started is True
    # Not even a status probe: a non-socket at the path is never "alive".
    assert [c for c in fake.calls if c[1] == "exec"][0][-1] != "--json"


def test_ensure_quota_rejects_with_user_message(monkeypatch, tmp_path: Path, podman_ok, profile: Path):
    decision = _decision(profile, podman_ok, max_containers_per_host=2)
    fake = _FakePodman(state=None, running=2)
    monkeypatch.setattr(ds, "_run_podman", fake)

    with pytest.raises(ds.DesktopSandboxError) as excinfo:
        ds.ensure(decision)
    assert excinfo.value.reason == "desktop_quota_exhausted"
    assert excinfo.value.user_message == "桌面名额已满，稍后再试"
    assert "run" not in [call[1] for call in fake.calls]


def test_ensure_quota_applies_to_start_of_stopped_container(monkeypatch, tmp_path: Path, podman_ok, profile: Path):
    decision = _decision(profile, podman_ok, max_containers_per_host=1)
    fake = _FakePodman(state="exited", running=1, labels=_labels_for(decision))
    monkeypatch.setattr(ds, "_run_podman", fake)

    with pytest.raises(ds.DesktopSandboxError) as excinfo:
        ds.ensure(decision)
    assert excinfo.value.reason == "desktop_quota_exhausted"
    assert "start" not in [call[1] for call in fake.calls]


def test_ensure_holds_host_lock_across_quota_and_create(monkeypatch, tmp_path: Path, podman_ok, profile: Path):
    decision = _decision(profile, podman_ok)
    host_lock = ds.state_root(decision.shared_home) / ds.HOST_LOCK_NAME
    profile_lock = decision.state_dir / ds.PROFILE_LOCK_NAME
    observed: dict[str, bool] = {}

    def _held(path: Path) -> bool:
        fd = os.open(path, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        finally:
            os.close(fd)

    fake = _FakePodman(state=None, screen_sock=decision.rfb_socket)
    fake.on_run = lambda: observed.update(host=_held(host_lock), profile=_held(profile_lock))
    monkeypatch.setattr(ds, "_run_podman", fake)

    ds.ensure(decision)
    assert observed == {"host": True, "profile": True}
    assert not _held(host_lock) and not _held(profile_lock)


def test_ensure_fails_closed_when_same_profile_ensure_is_in_flight(monkeypatch, tmp_path: Path, podman_ok, profile: Path):
    decision = _decision(profile, podman_ok)
    monkeypatch.setattr(ds, "PROFILE_LOCK_WAIT_S", 0.2)
    fake = _FakePodman(state="running", screen_sock=decision.rfb_socket, labels=_labels_for(decision))
    monkeypatch.setattr(ds, "_run_podman", fake)
    lock_path = decision.state_dir / ds.PROFILE_LOCK_NAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(ds.DesktopSandboxError) as excinfo:
            ds.ensure(decision)
        assert excinfo.value.reason == "desktop_container_unavailable"
        assert "lock" in str(excinfo.value)
        assert fake.calls == []
    finally:
        os.close(fd)


def test_ensure_rebuilds_container_whose_labels_drifted(monkeypatch, tmp_path: Path, podman_ok, profile: Path):
    decision = _decision(profile, podman_ok, image="img:new")
    stale = _labels_for(_decision(profile, podman_ok, image="img:old"))
    fake = _FakePodman(state="running", screen_sock=decision.rfb_socket, labels=stale)
    monkeypatch.setattr(ds, "_run_podman", fake)

    handle = ds.ensure(decision)

    assert handle.started is True
    verbs = [c[1] for c in fake.calls]
    assert verbs.index("rm") < verbs.index("ps") < verbs.index("run")
    assert fake.labels[ds.LABEL_SPEC] == _labels_for(decision)[ds.LABEL_SPEC]


def test_ensure_rebuilds_when_image_tag_was_rebuilt(monkeypatch, tmp_path: Path, podman_ok, profile: Path):
    decision = _decision(profile, podman_ok)
    fake = _FakePodman(state="running", screen_sock=decision.rfb_socket, labels=_labels_for(decision, image_id="sha256:old"), image_id="sha256:new")
    fake.container_image_id = "sha256:old"
    monkeypatch.setattr(ds, "_run_podman", fake)

    ds.ensure(decision)
    assert "rm" in [c[1] for c in fake.calls] and "run" in [c[1] for c in fake.calls]


def test_ensure_rebuilds_when_shared_config_inode_changed(monkeypatch, tmp_path: Path, podman_ok, profile: Path):
    shared = profile.parent.parent
    (shared / "config.yaml").write_text("model: {}\n")
    decision = _decision(profile, podman_ok)
    labels = _labels_for(decision)
    fake = _FakePodman(state="running", screen_sock=decision.rfb_socket, labels=labels)
    monkeypatch.setattr(ds, "_run_podman", fake)
    ds.ensure(decision)
    assert "rm" not in [c[1] for c in fake.calls]

    # Replace the inode (what editors and sed -i do), keep the content.
    (shared / "config.yaml.new").write_text("model: {}\n")
    os.replace(shared / "config.yaml.new", shared / "config.yaml")
    fake.calls.clear()
    ds.ensure(decision)
    assert "rm" in [c[1] for c in fake.calls] and "run" in [c[1] for c in fake.calls]


def test_ensure_defers_rebuild_while_a_turn_is_active(monkeypatch, tmp_path: Path, podman_ok, profile: Path):
    decision = _decision(profile, podman_ok, image="img:new")
    stale = _labels_for(_decision(profile, podman_ok, image="img:old"))
    fake = _FakePodman(state="running", screen_sock=decision.rfb_socket, labels=stale)
    _make_socket(decision.rfb_socket)
    monkeypatch.setattr(ds, "_run_podman", fake)
    token = ds.begin_turn(decision.state_dir)
    try:
        handle = ds.ensure(decision)
    finally:
        ds.end_turn(decision.state_dir, token)
    assert handle.started is False
    assert "rm" not in [c[1] for c in fake.calls]


def test_ensure_never_reuses_another_profiles_container(monkeypatch, tmp_path: Path, podman_ok, profile: Path):
    decision = _decision(profile, podman_ok)
    other = _labels_for(decision)
    other[ds.LABEL_PROFILE] = "someone_else"
    other[ds.LABEL_PROFILE_HOME] = str(tmp_path / "elsewhere")
    fake = _FakePodman(state="running", screen_sock=decision.rfb_socket, labels=other)
    monkeypatch.setattr(ds, "_run_podman", fake)
    ds.ensure(decision)
    assert [c[1] for c in fake.calls].count("rm") == 1


def test_ensure_screen_timeout_fails_closed_and_stops_what_it_started(monkeypatch, tmp_path: Path, podman_ok, profile: Path):
    decision = _decision(profile, podman_ok, screen_timeout_s=0.01)
    fake = _FakePodman(state="exited", screen_sock=None, labels=_labels_for(decision))  # exec succeeds but no socket
    monkeypatch.setattr(ds, "_run_podman", fake)

    with pytest.raises(ds.DesktopSandboxError) as excinfo:
        ds.ensure(decision)
    assert excinfo.value.reason == "desktop_screen_unavailable"
    verbs = [c[1] for c in fake.calls]
    assert verbs[-1] == "stop" and "rm" not in verbs  # started (not created) → stop only


def test_ensure_screen_failure_on_reused_running_container_does_not_stop_it(monkeypatch, tmp_path: Path, podman_ok, profile: Path):
    decision = _decision(profile, podman_ok)
    fake = _FakePodman(state="running", screen_sock=decision.rfb_socket, labels=_labels_for(decision), screen_running=False)
    fake.fail_screen_start = True
    _make_socket(decision.rfb_socket)
    monkeypatch.setattr(ds, "_run_podman", fake)
    with pytest.raises(ds.DesktopSandboxError) as excinfo:
        ds.ensure(decision)
    assert excinfo.value.reason == "desktop_screen_unavailable"
    assert "stop" not in [c[1] for c in fake.calls]


def test_ensure_container_up_budget_fails_closed(monkeypatch, tmp_path: Path, podman_ok, profile: Path):
    decision = _decision(profile, podman_ok, ensure_timeout_s=0.01)
    fake = _FakePodman(state=None, screen_sock=decision.rfb_socket)
    clock = {"now": 100.0}
    monkeypatch.setattr(ds.time, "monotonic", lambda: clock["now"])
    original_run = fake.__call__

    def slow_run(podman_bin, args, **kw):
        if args[0] == "run":
            clock["now"] += 5.0  # podman run took longer than the whole budget
        return original_run(podman_bin, args, **kw)

    monkeypatch.setattr(ds, "_run_podman", slow_run)
    with pytest.raises(ds.DesktopSandboxError) as excinfo:
        ds.ensure(decision)
    assert excinfo.value.reason == "desktop_container_unavailable"
    assert "budget" in str(excinfo.value) or "within" in str(excinfo.value)
    verbs = [c[1] for c in fake.calls]
    assert verbs[-2:] == ["stop", "rm"]


def test_inspect_error_other_than_missing_is_not_treated_as_absent(monkeypatch, tmp_path: Path, podman_ok, profile: Path):
    decision = _decision(profile, podman_ok)
    fake = _FakePodman(state="running", labels=_labels_for(decision))
    fake.inspect_error = "Error: cannot connect to Podman socket"
    monkeypatch.setattr(ds, "_run_podman", fake)
    with pytest.raises(ds.DesktopSandboxError) as excinfo:
        ds.ensure(decision)
    assert excinfo.value.reason == "desktop_container_unavailable"
    assert "run" not in [c[1] for c in fake.calls]


def test_ensure_wraps_quota_error_into_readable_runtime_error(monkeypatch, tmp_path: Path, podman_ok):
    agent_real = _linux(monkeypatch, tmp_path)
    profile = _desktop_profile(tmp_path, podman_ok)
    (tmp_path / ".hermes" / "config.yaml").write_text(
        f"multitenancy:\n  desktop:\n    podman_bin: {podman_ok}\n    max_containers_per_host: 1\n"
    )
    monkeypatch.setattr(ds, "_run_podman", _FakePodman(state=None, running=1))

    with pytest.raises(RuntimeError, match="桌面名额已满"):
        agent_real._wrap_with_sandbox([sys.executable, "child.py"], profile, env={"A": "b"})


# --- idle stop ----------------------------------------------------------------------------


def _ps_rows(*entries: tuple[str, Path]) -> str:
    return json.dumps([
        {
            "Names": [name],
            "Labels": {ds.LABEL_ROLE: ds.LABEL_ROLE_VALUE, ds.LABEL_PROFILE_HOME: str(home)},
        }
        for name, home in entries
    ])


def _state(shared: Path, home: Path, *, last_used: float, idle: int = 30) -> Path:
    state = ds.profile_state_dir(shared, home)
    ds.touch_last_used(state, now=last_used)
    (state / "meta.json").write_text(json.dumps({"idle_stop_minutes": idle}))
    return state


def test_idle_stop_sweep_uses_turn_leases_not_exec_ids(monkeypatch, tmp_path: Path, podman_ok):
    shared = tmp_path / ".hermes"
    idle_home = shared / "profiles" / "idle"
    busy_home = shared / "profiles" / "busy"
    fresh_home = shared / "profiles" / "fresh"
    foreign_home = shared / "profiles" / "foreign"
    now = 1_000_000.0
    _state(shared, idle_home, last_used=now - 31 * 60)
    busy = _state(shared, busy_home, last_used=now - 31 * 60)
    _state(shared, fresh_home, last_used=now - 5 * 60)
    ds.begin_turn(busy, now=now - 60)

    calls: list[list[str]] = []

    def fake_run(podman_bin, args, *, timeout=None, check=False):
        calls.append(args)
        if args[0] == "ps":
            return _completed(args, stdout=_ps_rows(
                ("hermes-p-idle", idle_home), ("hermes-p-busy", busy_home),
                ("hermes-p-fresh", fresh_home), ("hermes-p-foreign", foreign_home),
            ))
        if args[0] == "stop":
            return _completed(args)
        raise AssertionError(args)

    monkeypatch.setattr(ds, "_run_podman", fake_run)

    stopped = ds.idle_stop_sweep(podman_bin=podman_ok, shared_home=shared, now=now)

    assert stopped == ["hermes-p-idle"]
    assert [c for c in calls if c[0] == "stop"] == [["stop", "-t", "10", "hermes-p-idle"]]
    # A warm worker's permanent exec is irrelevant: no ExecIDs inspect at all.
    assert not any(c[0] == "inspect" for c in calls)


def test_idle_stop_sweep_leaked_lease_does_not_pin_container(monkeypatch, tmp_path: Path, podman_ok):
    shared = tmp_path / ".hermes"
    home = shared / "profiles" / "crashed"
    now = 2_000_000.0
    state = _state(shared, home, last_used=now - 4 * 3600)
    (state / "turns.json").write_text(json.dumps({"x": {"pid": os.getpid(), "started": now - 4 * 3600}}))

    def fake_run(podman_bin, args, *, timeout=None, check=False):
        if args[0] == "ps":
            return _completed(args, stdout=_ps_rows(("hermes-p-crashed", home)))
        if args[0] == "stop":
            return _completed(args)
        raise AssertionError(args)

    monkeypatch.setattr(ds, "_run_podman", fake_run)
    assert ds.idle_stop_sweep(podman_bin=podman_ok, shared_home=shared, now=now) == ["hermes-p-crashed"]


def test_idle_stop_sweep_honours_per_profile_minutes_and_zero_disables(monkeypatch, tmp_path: Path, podman_ok):
    shared = tmp_path / ".hermes"
    one = shared / "profiles" / "one"
    never = shared / "profiles" / "never"
    now = 2_000_000.0
    _state(shared, one, last_used=now - 2 * 60, idle=1)
    _state(shared, never, last_used=now - 10_000 * 60, idle=0)

    def fake_run(podman_bin, args, *, timeout=None, check=False):
        if args[0] == "ps":
            return _completed(args, stdout=_ps_rows(("hermes-p-one", one), ("hermes-p-never", never)))
        if args[0] == "stop":
            return _completed(args)
        raise AssertionError(args)

    monkeypatch.setattr(ds, "_run_podman", fake_run)
    assert ds.idle_stop_sweep(podman_bin=podman_ok, shared_home=shared, now=now) == ["hermes-p-one"]


def test_idle_stop_sweep_skips_profile_whose_ensure_is_in_flight(monkeypatch, tmp_path: Path, podman_ok):
    shared = tmp_path / ".hermes"
    home = shared / "profiles" / "starting"
    now = 3_000_000.0
    state = _state(shared, home, last_used=now - 60 * 60)
    stops: list[list[str]] = []

    def fake_run(podman_bin, args, *, timeout=None, check=False):
        if args[0] == "ps":
            return _completed(args, stdout=_ps_rows(("hermes-p-starting", home)))
        stops.append(args)
        return _completed(args)

    monkeypatch.setattr(ds, "_run_podman", fake_run)
    fd = os.open(state / ds.PROFILE_LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        assert ds.idle_stop_sweep(podman_bin=podman_ok, shared_home=shared, now=now) == []
    finally:
        os.close(fd)
    assert stops == []


def test_idle_stop_sweep_is_noop_without_podman_or_shared_home(tmp_path: Path, monkeypatch):
    assert ds.idle_stop_sweep(podman_bin=str(tmp_path / "missing-podman")) == []
    fake_bin = tmp_path / "podman"
    fake_bin.write_text("#!/bin/sh\nexit 0\n")
    fake_bin.chmod(0o755)
    monkeypatch.delenv("HERMES_SHARED_HOME", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setattr(ds, "_run_podman", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not list")))
    assert ds.idle_stop_sweep(podman_bin=str(fake_bin)) == []


def test_idle_sweep_thread_starts_once_on_linux_only(monkeypatch):
    ds.stop_desktop_idle_sweeps()
    monkeypatch.setattr(sys, "platform", "darwin")
    ds.ensure_desktop_idle_sweeps_started(interval=3600)
    assert ds._sweep_thread is None

    monkeypatch.setattr(sys, "platform", "linux")
    seen: list[dict] = []
    monkeypatch.setattr(ds, "idle_stop_sweep", lambda **kw: seen.append(kw) or [])
    try:
        ds.ensure_desktop_idle_sweeps_started(interval=3600, podman_bin="/opt/podman", shared_home=Path("/srv/x"))
        first = ds._sweep_thread
        ds.ensure_desktop_idle_sweeps_started(interval=3600)
        assert ds._sweep_thread is first and first.is_alive()
        first.join(timeout=0.1)
    finally:
        ds.stop_desktop_idle_sweeps()
    assert ds._sweep_thread is None
    assert seen and seen[0] == {"podman_bin": "/opt/podman", "shared_home": Path("/srv/x")}


def test_plugin_entry_wires_desktop_idle_sweeps_with_shared_podman_bin():
    source = (Path(__file__).resolve().parent.parent / "hermes_multitenancy" / "plugin_entry.py").read_text(encoding="utf-8")
    assert "host_desktop_settings(_desktop_shared_home).podman_bin" in source
    assert "ensure_desktop_idle_sweeps_started(\n" in source


def test_run_args_rejects_owner_uid_inside_subordinate_range(monkeypatch, profile: Path):
    decision = ds.desktop_decision(
        {"multitenancy": {"desktop": {"enabled": True, "subid_base": 65536}}}, profile
    )
    monkeypatch.setattr(ds, "_selinux_enforcing", lambda: False)
    fake_stat = SimpleNamespace(st_uid=70000, st_gid=70000)
    monkeypatch.setattr(Path, "stat", lambda self, *a, **k: fake_stat)
    with pytest.raises(ds.DesktopSandboxError) as excinfo:
        ds.run_args(decision)
    assert excinfo.value.reason == "desktop_container_unavailable"


def test_profile_model_capability_declarations_are_not_desktop_host_keys(profile: Path, caplog):
    """``model.supports_vision`` is a model-capability declaration core reads from the
    profile's own config.yaml (HERMES_HOME/config.yaml inside the worker), not a
    ``multitenancy.desktop`` host key: a profile may set it, and it is never warned about."""
    assert "supports_vision" not in ds.HOST_ONLY_KEYS
    with caplog.at_level("WARNING", logger="hermes_multitenancy.desktop_sandbox"):
        decision = ds.desktop_decision(
            {}, profile,
            profile_config={
                "model": {"supports_vision": True},
                "agent": {"image_input_mode": "native"},
                "multitenancy": {"desktop": {"enabled": True}},
            },
        )
    assert decision.enabled is True
    assert decision.ignored_profile_keys == ()
    assert not [rec for rec in caplog.records if "host-only" in rec.message]


# --- rootless podman (gateway is not root) --------------------------------------


def _rootless(monkeypatch, profile: Path) -> None:
    """Gateway runs as the profile owner, not root: ``auto`` resolves to rootless."""
    uid = profile.stat().st_uid
    monkeypatch.setattr(ds, "_effective_uid", lambda: uid if uid != 0 else 1000)


def _rootful_snapshot_layout(tmp_path: Path) -> tuple[Path, Path]:
    shared = tmp_path / ".hermes"
    profile = (shared / "profiles" / "desktop_smoke")
    (profile / "workspace").mkdir(parents=True)
    (shared / "config.yaml").write_text("model: {}\n")
    (shared / ".env").write_text("SHARED=1\n")
    (shared / "bin").mkdir()
    (shared / "skills").mkdir()
    (shared / "cron").mkdir()
    return shared.resolve(), profile.resolve()


def test_rootful_run_args_snapshot_is_byte_identical_to_pre_rootless(monkeypatch, tmp_path: Path):
    """Regression guard: a root gateway builds exactly the pre-rootless argv.

    The expected list is the argv origin/main (86ac04d5) produced for this
    layout, written out literally; the spec digest is recomputed with the
    pre-rootless spec dict so an added key would show up as drift here. The
    only intended changes since: every tmpfs carries ``notmpcopyup`` and the
    spec records it.
    """
    shared, profile = _rootful_snapshot_layout(tmp_path)
    monkeypatch.setattr(ds, "_selinux_enforcing", lambda: True)
    decision = ds.desktop_decision(DESKTOP_ON, profile)
    owner = profile.stat()
    cfg = (shared / "config.yaml").stat()
    mt_repo = Path(ds.__file__).resolve().parent.parent
    spec = {
        "image": ds.DEFAULT_IMAGE, "network": "hermes-desktop", "subid_base": 300000,
        "owner": [owner.st_uid, owner.st_gid], "memory": "3g", "cpus": "2", "shm_size": "1g",
        "container_python": ds.DEFAULT_CONTAINER_PYTHON, "container_hermes": ds.DEFAULT_CONTAINER_HERMES,
        "container_path": ds.DEFAULT_CONTAINER_PATH,
        "exec_env": [["XDG_RUNTIME_DIR", "/tmp/hermes-runtime"]],
        "tmpfs": ["feishu_uat", "tokens", "workspace/credentials", "home"],
        "tmpfs_opts": "notmpcopyup",
    }
    spec_hash = hashlib.sha256(json.dumps(spec, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    p, s = str(profile), str(shared)
    expected = [
        "run", "-d", "--name", decision.container_name, "--init", "--entrypoint", "/bin/sleep",
        "--user", "10000:10000",
        "--uidmap", "0:300000:10000", "--uidmap", f"10000:{owner.st_uid}:1", "--uidmap", "10001:310001:55535",
        "--gidmap", "0:300000:10000", "--gidmap", f"10000:{owner.st_gid}:1", "--gidmap", "10001:310001:55535",
        "--label", "io.hermes.mt.role=desktop",
        "--label", "io.hermes.mt.profile=desktop_smoke",
        "--label", f"io.hermes.mt.profile_home={p}",
        "--label", f"io.hermes.mt.spec={spec_hash}",
        "--label", "io.hermes.mt.image_id=sha256:img",
        "--label", f"io.hermes.mt.shared_config={cfg.st_dev}:{cfg.st_ino}",
        "--memory", "3g", "--cpus", "2", "--shm-size", "1g", "--network", "hermes-desktop",
        "--security-opt", "no-new-privileges", "--cap-drop", "ALL",
        "-e", "HERMES_UID=10000", "-e", "HERMES_GID=10000", "-e", f"HERMES_HOME={p}",
        "-v", f"{p}:{p}:rw,Z",
        "-v", f"{p}/workspace:/workspace:rw,Z",
        "-v", f"{mt_repo}:{mt_repo}:ro,z",
        "--mount", f"type=bind,src=/dev/null,dst={p}/.env,ro",
        "--mount", f"type=bind,src={mt_repo}/hermes_multitenancy/sandbox/empty-auth.json,dst={p}/auth.json,ro,relabel=shared",
        "--tmpfs", "/tmp/hermes-runtime:notmpcopyup",
        "--tmpfs", f"{p}/feishu_uat:notmpcopyup", "--tmpfs", f"{p}/tokens:notmpcopyup",
        "--tmpfs", f"{p}/workspace/credentials:notmpcopyup", "--tmpfs", f"{p}/home:notmpcopyup",
        "--mount", f"type=bind,src={s}/config.yaml,dst={s}/config.yaml,ro,relabel=shared",
        "--mount", f"type=bind,src=/dev/null,dst={s}/.env,ro",
        "-v", f"{s}/bin:{s}/bin:ro,z",
        "-v", f"{s}/skills:{s}/skills:ro,z",
        "-v", f"{s}/cron:{s}/cron:rw,z",
        ds.DEFAULT_IMAGE, "infinity",
    ]

    assert ds.run_args(decision, image_id="sha256:img") == expected
    # Forcing rootful while not root gives the same bytes (only the mode decides).
    monkeypatch.setattr(ds, "_effective_uid", lambda: 4242)
    forced = ds.desktop_decision(_shared_on("podman", rootless="false"), profile)
    assert ds.run_args(forced, image_id="sha256:img") == expected
    assert decision.spec_hash(owner.st_uid, owner.st_gid) == spec_hash


@pytest.mark.parametrize("rootless", [False, True])
def test_exec_spawn_env_never_carries_tenant_loader_or_engine_controls(
    monkeypatch, tmp_path: Path, podman_ok, profile: Path, rootless: bool,
):
    """P0 (review 2026-10-08): the host podman process must never see the worker env.

    The worker env carries the tenant's profile .env; LD_PRELOAD/LD_AUDIT there
    would load tenant code into the HOST podman (root for rootful) before any
    sandbox exists, and CONTAINERS_*/PODMAN_* would redirect podman itself.
    """
    if rootless:
        _rootless(monkeypatch, profile)
    for name in list(os.environ):
        if name.startswith(("LD_", "CONTAINERS_", "_CONTAINERS_", "CONTAINER_", "PODMAN_", "BUILDAH_", "XDG_")) or name in ds.PODMAN_HOST_ENV:
            monkeypatch.delenv(name)
    monkeypatch.setenv("HOME", "/home/gateway")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/gateway/lib")
    monkeypatch.setenv("CONTAINERS_STORAGE_CONF", "/etc/gateway-storage.conf")
    monkeypatch.setenv("GATEWAY_ONLY_SECRET", "gw-secret")
    decision = _decision(profile, podman_ok)
    fake = _FakePodman(state=None, screen_sock=decision.rfb_socket)
    monkeypatch.setattr(ds, "_run_podman", fake)
    handle = ds.ensure(decision)
    worker_env = {
        "LD_PRELOAD": f"{profile}/evil.so",
        "LD_AUDIT": f"{profile}/audit.so",
        "LD_LIBRARY_PATH": f"{profile}/lib",
        "DYLD_INSERT_LIBRARIES": f"{profile}/evil.dylib",
        "GCONV_PATH": f"{profile}/gconv",
        "GLIBC_TUNABLES": "glibc.malloc.check=3",
        "PYTHONPATH": "/opt/hermes-agent",
        "CONTAINERS_CONF": f"{profile}/containers.conf",
        "CONTAINERS_STORAGE_CONF": f"{profile}/storage.conf",
        "_CONTAINERS_ROOTLESS_UID": "0",
        "PODMAN_USERNS": "host",
        "BUILDAH_ISOLATION": "chroot",
        "CONTAINER_HOST": "unix:///tenant.sock",
        "STORAGE_DRIVER": "vfs",
        "GODEBUG": "x=1",
        "HOME": f"{profile}/home",
        "XDG_CONFIG_HOME": f"{profile}/config",
        "TMPDIR": f"{profile}/tmp",
        "OPENAI_API_KEY": "sk-secret-value",
        "HERMES_HOME": str(profile),
        "PATH": "/host/bin",
    }

    argv, spawn_env = ds.exec_args(decision, handle, ["/py", "x.py"], worker_env, workdir=profile / "workspace")

    assert argv[:3] == [podman_ok, "exec", "-i"]
    # Host spawn env: gateway podman paths + gateway engine config + by-name worker keys. Nothing else.
    assert spawn_env == {
        "HOME": "/home/gateway",
        "PATH": "/usr/bin:/bin",
        "XDG_RUNTIME_DIR": "/run/user/1000",
        "CONTAINERS_STORAGE_CONF": "/etc/gateway-storage.conf",
        "OPENAI_API_KEY": "sk-secret-value",
        "HERMES_HOME": str(profile),
    }
    # Secrets cross by name only.
    assert "sk-secret-value" not in " ".join(argv)
    assert argv[argv.index("OPENAI_API_KEY") - 1] == "-e"
    # Tenant paths and loader/interpreter values reach the CONTAINER inline only.
    for key in ("HOME", "XDG_CONFIG_HOME", "TMPDIR", "LD_PRELOAD", "LD_AUDIT", "PYTHONPATH", "GCONV_PATH"):
        assert f"{key}={worker_env[key]}" in argv
        assert key not in argv  # never a bare by-name -e KEY
    # Container-engine controls go nowhere.
    joined = " ".join(argv)
    for key in ("CONTAINERS_CONF", "_CONTAINERS_ROOTLESS_UID", "PODMAN_USERNS", "BUILDAH_ISOLATION",
                "CONTAINER_HOST", "STORAGE_DRIVER", "GODEBUG"):
        assert key not in joined
    assert f"{profile}/storage.conf" not in joined


def test_sandbox_spawn_desktop_profile_spawns_with_spawn_env_not_worker_env(
    monkeypatch, tmp_path: Path, podman_ok,
):
    agent_real = _linux(monkeypatch, tmp_path)
    profile = _desktop_profile(tmp_path, podman_ok)
    decision = agent_real._desktop_decision_for_profile(profile)
    fake = _FakePodman(state=None, screen_sock=decision.rfb_socket)
    monkeypatch.setattr(ds, "_run_podman", fake)
    worker_env = {"LD_PRELOAD": f"{profile}/evil.so", "OPENAI_API_KEY": "sk-x"}

    argv, spawn_env = agent_real._sandbox_spawn([sys.executable, "child.py"], profile, env=worker_env)

    assert spawn_env is not worker_env
    assert "LD_PRELOAD" not in spawn_env and spawn_env["OPENAI_API_KEY"] == "sk-x"
    assert f"LD_PRELOAD={profile}/evil.so" in argv
    # The argv-only view is the same argv.
    assert agent_real._wrap_with_sandbox([sys.executable, "child.py"], profile, env=worker_env) == argv


def test_sandbox_spawn_non_desktop_profile_keeps_worker_env_and_bwrap_argv(monkeypatch, tmp_path: Path):
    agent_real = _linux(monkeypatch, tmp_path)
    profile = tmp_path / ".hermes" / "profiles" / "alice"
    profile.mkdir(parents=True)
    env = {"A": "1"}

    argv, spawn_env = agent_real._sandbox_spawn(["/usr/bin/python3", "c.py"], profile, env=env)

    assert spawn_env is env
    assert argv == agent_real._wrap_linux_bwrap(["/usr/bin/python3", "c.py"], profile)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, "auto"), ("auto", "auto"), (True, "true"), ("true", "true"), ("on", "true"),
     (False, "false"), ("false", "false"), ("off", "false"), ("bogus", "auto")],
)
def test_rootless_mode_parsing(profile: Path, value, expected):
    extra = {} if value is None else {"rootless": value}
    assert ds.desktop_decision(_shared_on("podman", **extra), profile).rootless == expected


@pytest.mark.parametrize(("mode", "euid", "expected"), [
    ("auto", 0, False), ("auto", 1000, True), ("true", 0, True), ("false", 1000, False),
])
def test_is_rootless_follows_mode_then_euid(monkeypatch, profile: Path, mode, euid, expected):
    monkeypatch.setattr(ds, "_effective_uid", lambda: euid)
    decision = ds.desktop_decision(_shared_on("podman", rootless=mode), profile)
    assert ds.is_rootless(decision) is expected


def test_rootless_run_args_use_keep_id_and_keep_every_hardening_flag(monkeypatch, tmp_path: Path):
    shared, profile = _rootful_snapshot_layout(tmp_path)
    monkeypatch.setattr(ds, "_selinux_enforcing", lambda: True)
    rootful = ds.run_args(ds.desktop_decision(DESKTOP_ON, profile), image_id="sha256:img")
    _rootless(monkeypatch, profile)
    decision = ds.desktop_decision(DESKTOP_ON, profile)

    args = ds.run_args(decision, image_id="sha256:img")

    assert "--uidmap" not in args and "--gidmap" not in args
    assert "--userns=keep-id:uid=10000,gid=10000" in args
    assert args[args.index("--userns=keep-id:uid=10000,gid=10000") - 1] == "10000:10000"
    # Every tmpfs is owned by uid 10000 (keep-id leaves it to container root otherwise).
    tmpfs = [args[i + 1] for i, arg in enumerate(args[:-1]) if arg == "--tmpfs"]
    assert len(tmpfs) == 1 + len(ds.TMPFS_PROFILE_SUBDIRS)
    assert all(t.endswith(":rw,mode=0700,U,notmpcopyup") for t in tmpfs)
    assert f"{ds.CONTAINER_RUNTIME_DIR}:rw,mode=0700,U,notmpcopyup" in tmpfs
    # Everything but the id maps and the spec digest is byte-identical to rootful.
    def strip(argv: list[str]) -> list[str]:
        out, skip = [], 0
        for i, arg in enumerate(argv):
            if skip:
                skip -= 1
                continue
            if arg in {"--uidmap", "--gidmap"}:
                skip = 1
                continue
            if arg.startswith("--userns=") or arg.startswith(f"{ds.LABEL_SPEC}="):
                continue
            out.append(arg.removesuffix(":rw,mode=0700,U,notmpcopyup").removesuffix(":notmpcopyup"))
        return out
    assert strip(args) == strip(rootful)
    joined = " ".join(args)
    for flag in ("--cap-drop ALL", "--security-opt no-new-privileges", "--user 10000:10000",
                 f"type=bind,src=/dev/null,dst={profile}/.env,ro", f"--tmpfs {profile}/tokens:"):
        assert flag in joined
    owner = profile.stat()
    labels = _labels_from_run_args(args)
    assert labels[ds.LABEL_SPEC] == decision.spec_hash(owner.st_uid, owner.st_gid, rootless=True)
    assert labels[ds.LABEL_SPEC] != decision.spec_hash(owner.st_uid, owner.st_gid)


def test_rootless_run_args_refuse_profile_not_owned_by_gateway(monkeypatch, profile: Path):
    monkeypatch.setattr(ds, "_selinux_enforcing", lambda: False)
    monkeypatch.setattr(ds, "_effective_uid", lambda: profile.stat().st_uid + 1)
    decision = ds.desktop_decision(_shared_on("podman", rootless="true"), profile)

    with pytest.raises(ds.DesktopSandboxError) as excinfo:
        ds.run_args(decision)
    assert excinfo.value.reason == "desktop_container_unavailable"
    assert "keep-id can only map the gateway user" in str(excinfo.value)


def test_rootless_ensure_probes_podman_info_first_and_pins_gateway_env_for_exec(
    monkeypatch, tmp_path: Path, podman_ok, profile: Path,
):
    _rootless(monkeypatch, profile)
    monkeypatch.setenv("HOME", "/home/gateway")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    decision = _decision(profile, podman_ok)
    fake = _FakePodman(state=None, screen_sock=decision.rfb_socket)
    monkeypatch.setattr(ds, "_run_podman", fake)

    handle = ds.ensure(decision)

    assert fake.calls[0][1:] == ["info", "--format", "{{.Host.Security.Rootless}}"]
    run = next(call for call in fake.calls if call[1] == "run")
    assert "--userns=keep-id:uid=10000,gid=10000" in run and "--uidmap" not in run

    worker_env = {
        "HOME": f"{profile}/home",
        "XDG_CONFIG_HOME": f"{profile}/config",
        "XDG_RUNTIME_DIR": "/should-not-cross",
        "OPENAI_API_KEY": "sk-secret-value",
        "PATH": "/host/bin",
    }
    argv, spawn_env = ds.exec_args(decision, handle, ["/py", "x.py"], worker_env, workdir=profile / "workspace")

    # The host podman finds its storage through the gateway's HOME/XDG, not the tenant's.
    assert spawn_env["HOME"] == "/home/gateway" and spawn_env["XDG_RUNTIME_DIR"] == "/run/user/1000"
    assert "XDG_CONFIG_HOME" not in spawn_env and "XDG_DATA_HOME" not in spawn_env
    assert argv[:3] == [podman_ok, "exec", "-i"]
    assert f"HOME={profile}/home" in argv and f"XDG_CONFIG_HOME={profile}/config" in argv
    assert f"XDG_RUNTIME_DIR={ds.CONTAINER_RUNTIME_DIR}" in argv and "XDG_RUNTIME_DIR=/should-not-cross" not in argv
    assert argv[argv.index("OPENAI_API_KEY") - 1] == "-e"
    assert "sk-secret-value" not in " ".join(argv)


@pytest.mark.parametrize(("returncode", "stdout"), [(125, ""), (0, "false\n")])
def test_rootless_ensure_fails_closed_when_podman_info_fails_or_is_rootful(
    monkeypatch, tmp_path: Path, podman_ok, profile: Path, returncode: int, stdout: str,
):
    _rootless(monkeypatch, profile)
    decision = _decision(profile, podman_ok)
    fake = _FakePodman(state=None, screen_sock=decision.rfb_socket)
    fake.info_returncode, fake.info_stdout = returncode, stdout
    monkeypatch.setattr(ds, "_run_podman", fake)

    with pytest.raises(ds.DesktopSandboxError) as excinfo:
        ds.ensure(decision)
    assert excinfo.value.reason == "desktop_podman_unavailable"
    assert [call[1] for call in fake.calls] == ["info"]


def test_wrap_rootless_podman_info_failure_emits_security_event(monkeypatch, tmp_path: Path):
    """Acceptance: podman info fails (PATH points at a broken podman) → fail-closed + event."""
    agent_real = _linux(monkeypatch, tmp_path)
    audit = _audit(monkeypatch, tmp_path)
    broken = tmp_path / "empty-bin" / "podman"
    broken.parent.mkdir()
    broken.write_text("#!/bin/sh\necho 'cannot find newuidmap' >&2\nexit 125\n")
    broken.chmod(0o755)
    profile = _desktop_profile(tmp_path, str(broken))
    _rootless(monkeypatch, profile)

    with pytest.raises(RuntimeError, match="desktop_podman_unavailable"):
        agent_real._wrap_with_sandbox([sys.executable, "child.py"], profile, env={"A": "b"})

    events = _audit_events(audit)
    assert events[-1]["event_type"] == "sandbox.denied"
    assert events[-1]["reason"] == "desktop_podman_unavailable"


@pytest.mark.parametrize("rootless", [True, False])
def test_podman_run_timeout_reason_is_neutral_and_rootless_only(
    monkeypatch, tmp_path: Path, podman_ok, profile: Path, rootless: bool,
):
    if rootless:
        _rootless(monkeypatch, profile)
    decision = _decision(profile, podman_ok)
    fake = _FakePodman(state=None, screen_sock=decision.rfb_socket)

    def slow_run(podman_bin, args, *, timeout=None, check=False):
        if args[0] == "run":
            raise subprocess.TimeoutExpired(args, timeout)
        return fake(podman_bin, args, timeout=timeout, check=check)

    monkeypatch.setattr(ds, "_run_podman", slow_run)

    with pytest.raises(ds.DesktopSandboxError) as excinfo:
        ds.ensure(decision)
    if rootless:
        # The timeout does not say why; the message must not pin it on prewarm.
        assert excinfo.value.reason == "desktop_podman_run_timeout"
        message = str(excinfo.value)
        assert "crun create" in message and "Possible causes" in message
        assert "Possible causes: tmpfs copy-up of a large masked directory" in message
        assert "SELinux relabel" in message
        assert "podman run --rm --userns=keep-id:uid=10000,gid=10000 --entrypoint /bin/true" in message
        assert "prewarm_required" not in message
    else:
        assert excinfo.value.reason == "desktop_container_unavailable"
        assert "prewarm" not in str(excinfo.value)


@pytest.mark.parametrize("rootless", [True, False])
def test_every_tmpfs_is_created_without_copyup(monkeypatch, tmp_path: Path, rootless: bool):
    """podman's default tmpcopyup would copy each masked directory into its tmpfs."""
    shared, profile = _rootful_snapshot_layout(tmp_path)
    if rootless:
        _rootless(monkeypatch, profile)
    args = ds.run_args(ds.desktop_decision(DESKTOP_ON, profile), image_id="sha256:img")

    tmpfs = [args[i + 1] for i, arg in enumerate(args[:-1]) if arg == "--tmpfs"]
    assert len(tmpfs) == 1 + len(ds.TMPFS_PROFILE_SUBDIRS)
    for spec in tmpfs:
        _, _, opts = spec.partition(":")
        assert "notmpcopyup" in opts.split(","), spec
        assert "tmpcopyup" not in opts.split(","), spec
    # No other mount type re-introduces a tmpfs with copy-up.
    assert not any("type=tmpfs" in arg for arg in args)


@pytest.mark.parametrize("rootless", [True, False])
def test_secret_dirs_are_masked_by_empty_tmpfs(monkeypatch, tmp_path: Path, rootless: bool):
    """The UAT token, tokens and credentials on the host never reach the container.

    Each secret directory must be the target of a copy-up-free tmpfs, and no
    bind mount may expose it (or a file in it) at another path.
    """
    shared, profile = _rootful_snapshot_layout(tmp_path)
    for rel in ds.TMPFS_PROFILE_SUBDIRS:
        (profile / rel).mkdir(parents=True, exist_ok=True)
    (profile / "feishu_uat" / "ou_secret.json").write_text('{"access_token": "u-secret"}')
    (profile / "tokens" / "gitlab.json").write_text("glpat-secret")
    (profile / "workspace" / "credentials" / "key.json").write_text("cred-secret")
    if rootless:
        _rootless(monkeypatch, profile)
    args = ds.run_args(ds.desktop_decision(DESKTOP_ON, profile), image_id="sha256:img")

    masks = {}
    for i, arg in enumerate(args[:-1]):
        if arg == "--tmpfs":
            path, _, opts = args[i + 1].partition(":")
            masks[path] = opts.split(",")
    for rel in ("feishu_uat", "tokens", "workspace/credentials"):
        target = str(profile / rel)
        assert "notmpcopyup" in masks[target], rel
    sources = []
    for i, arg in enumerate(args[:-1]):
        if arg == "-v":
            sources.append(args[i + 1].split(":")[0])
        elif arg == "--mount":
            fields = dict(f.split("=", 1) for f in args[i + 1].split(",") if "=" in f)
            sources.append(fields.get("src", ""))
    for src in sources:
        for rel in ("feishu_uat", "tokens", "workspace/credentials"):
            secret = profile / rel
            # A bind of the directory itself or anything below it would bypass the mask.
            assert not Path(src).is_relative_to(secret), (src, rel)
