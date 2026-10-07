"""Missing upstream admission support must fail closed before dispatch."""
import pytest

import hermes_multitenancy as mt
import hermes_multitenancy.plugin_entry as pe
import hermes_multitenancy.trusted_feishu_ingress as tfi


@pytest.mark.parametrize("reason", [
    "Feishu core lacks trusted ingress contract",
    "live Feishu adapter module did not materialize",
    "unexpected adapter failure",
])
def test_missing_admission_aborts_install_and_cannot_bypass_dispatch(monkeypatch, reason):
    def fail():
        raise RuntimeError(reason)
    monkeypatch.setattr(tfi, "install_trusted_feishu_ingress_admission", fail)
    with pytest.raises(RuntimeError, match=reason):
        pe._install_trusted_feishu_ingress()
    monkeypatch.setattr(tfi, "validate_admitted_feishu_event", lambda *args: False)
    def forbidden(**kwargs):
        pytest.fail("unadmitted event reached routing")
    monkeypatch.setattr(mt, "on_pre_gateway_dispatch", forbidden)
    assert pe._dispatch_with_worker_init(event=object(), gateway=object()) == {
        "action": "skip", "reason": "trusted Feishu ingress denied",
    }


def test_installed_admission_keeps_dispatch_validation(monkeypatch):
    monkeypatch.setattr(tfi, "install_trusted_feishu_ingress_admission", lambda: None)
    pe._install_trusted_feishu_ingress()
    monkeypatch.setattr(tfi, "validate_admitted_feishu_event", lambda *args: False)
    assert pe._dispatch_with_worker_init(event=object(), gateway=object())["action"] == "skip"


def test_real_core_loader_cannot_swallow_missing_admission(monkeypatch):
    from hermes_cli.plugins import PluginManager, PluginManifest

    def fail():
        raise RuntimeError("Feishu core lacks trusted ingress contract")

    monkeypatch.setattr(tfi, "install_trusted_feishu_ingress_admission", fail)
    monkeypatch.setattr(mt, "_register", lambda ctx: pe._install_trusted_feishu_ingress())
    manager = PluginManager()
    monkeypatch.setattr(manager, "_load_entrypoint_module", lambda manifest: mt)
    with pytest.raises(SystemExit) as stopped:
        manager._load_plugin(PluginManifest(name="multitenancy", source="entrypoint"))
    assert stopped.value.code == 1
