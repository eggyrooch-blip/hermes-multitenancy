import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_runner():
    path = ROOT / "scripts" / "feishu_file_media_matrix_runner.py"
    spec = importlib.util.spec_from_file_location("feishu_file_media_matrix_runner", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_outbound_prompt_is_natural_and_does_not_expose_internal_paths():
    runner = _load_runner()
    item = {
        "kind": "json",
        "workspace_path": "/workspace/Downloads/out.json",
        "marker": "FEISHU_MEDIA_OUT_CONTENT_JSON_20260519",
        "instruction": "请生成一个合法 JSON 文件并发给我，顶层字段 marker 必须等于 FEISHU_MEDIA_OUT_CONTENT_JSON_20260519。",
    }

    prompt = runner._outbound_prompt(item, "FEISHU_MEDIA_OUT_JSON_20260519")

    assert "```hermes-artifact-json" not in prompt
    assert "/workspace" not in prompt
    assert "MEDIA:" not in prompt
    assert "第一行必须" not in prompt
    assert "不要只在聊天" not in prompt
    assert "真实文件出站验收" not in prompt
    assert "发给我" in prompt
    assert item["marker"] in prompt


def test_outbound_image_artifact_spec_requests_document_delivery_without_path():
    runner = _load_runner()
    item = {
        "kind": "png",
        "workspace_path": "/workspace/Downloads/out.png",
        "filename": "out.png",
        "marker": "FEISHU_MEDIA_OUT_CONTENT_PNG_20260519",
        "image": True,
    }

    spec = runner._outbound_artifact_spec(item)

    assert spec["filename"] == "out.png"
    assert "path" not in spec
    assert spec["as_document"] is True


def test_outbound_artifact_specs_cover_structured_formats():
    runner = _load_runner()

    by_kind = {
        item["kind"]: runner._outbound_artifact_spec(item)
        for item in runner._make_outbound_files("20260519")
    }

    assert by_kind["xlsx"]["rows"][1][0] == by_kind["xlsx"]["marker"]
    assert by_kind["docx"]["content"].find(by_kind["docx"]["marker"]) >= 0
    assert by_kind["pdf"]["content"].find(by_kind["pdf"]["marker"]) >= 0
    assert by_kind["png"]["marker"] == "FEISHU_MEDIA_OUT_CONTENT_PNG_20260519"
    assert by_kind["json"]["data"]["marker"] == "FEISHU_MEDIA_OUT_CONTENT_JSON_20260519"
    assert by_kind["json"]["filename"] == "hermes_media_out_json_20260519.json"
    assert "path" not in by_kind["json"]


def test_outbound_raw_media_directive_is_not_counted_as_host_path_leak():
    runner = _load_runner()

    assert not runner._path_leaked("MEDIA:/workspace/Downloads/out.md")
    assert runner._path_leaked("MEDIA:/Users/dev/.hermes/profiles/feishu_x/.env")


def test_list_messages_retries_transient_feishu_502(monkeypatch):
    runner = _load_runner()
    calls = {"count": 0}

    def fake_run_lark(rt, args, *, timeout=90):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError('{"error":{"code":502,"message":"HTTP 502"}}')
        return {"data": {"messages": [{"message_id": "om_ok"}]}}

    monkeypatch.setattr(runner, "_run_lark", fake_run_lark)

    assert runner._list_messages(object()) == [{"message_id": "om_ok"}]
    assert calls["count"] == 2


def test_list_messages_can_filter_from_start_time(monkeypatch):
    runner = _load_runner()
    captured = {}

    def fake_run_lark(rt, args, *, timeout=90):
        captured["args"] = args
        return {"data": {"messages": []}}

    monkeypatch.setattr(runner, "_run_lark", fake_run_lark)

    assert runner._list_messages(object(), start=1779201965.0) == []
    assert "--start" in captured["args"]
    assert "2026-" in captured["args"][captured["args"].index("--start") + 1]


def test_send_text_retries_transient_feishu_502(monkeypatch):
    runner = _load_runner()
    calls = {"count": 0}

    def fake_run_lark(rt, args, *, timeout=90):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError('{"error":{"type":"http_error","message":"HTTP 502: forward request failed"}}')
        return {"data": {"message_id": "om_sent"}}

    monkeypatch.setattr(runner, "_run_lark", fake_run_lark)

    assert runner._send_text(object(), "hello", "mark")["data"]["message_id"] == "om_sent"
    assert calls["count"] == 2
