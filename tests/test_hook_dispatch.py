"""US-002 verification: hook callback fires asyncio.create_task and returns skip."""
from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
import json
import zlib
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_multitenancy import router as router_mod


def test_file_generation_guidance_uses_profile_downloads_not_tmp():
    guidance = router_mod._LARK_CLI_SOUL_GUIDANCE
    assert "execute_code" in guidance
    assert "/workspace/Downloads" in guidance
    assert "/tmp" in guidance


def _build_event(
    text: str = "hi",
    chat_id: str = "chat-123",
    user_id: str = "ou_test",
    sender_open_id: str | None = None,
):
    """Build a minimal MessageEvent-shaped object for hook testing."""
    event = SimpleNamespace(
        text=text,
        source=SimpleNamespace(
            chat_id=chat_id,
            user_id=user_id,
            user_name="test-user",
            chat_type="dm",
            platform=SimpleNamespace(value="feishu"),
        ),
    )
    if sender_open_id is not None:
        event.sender_open_id = sender_open_id
    return event


def test_profile_scoped_media_response_rewrites_temp_path_to_profile_artifact(tmp_path):
    """External temp MEDIA tags publish profile artifacts into the WebUI workspace."""
    from hermes_multitenancy import router as router_mod

    profile_home = tmp_path / "profiles" / "owner"
    source = profile_home / "home" / "Downloads" / "logo.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"png")
    workspace_artifact = profile_home / "workspace" / "Downloads" / "logo.png"

    response = router_mod._profile_scoped_media_response(
        "created\nMEDIA:/tmp/logo/logo.png",
        profile_home,
    )

    assert response == f"created\nMEDIA:{workspace_artifact.resolve()}"
    assert workspace_artifact.read_bytes() == b"png"


def test_profile_scoped_media_response_publishes_direct_profile_home_file(tmp_path):
    """Files generated directly under profile home are published for WebUI chat downloads."""
    from hermes_multitenancy import router as router_mod

    profile_home = tmp_path / "profiles" / "owner"
    source = profile_home / "home" / "generated_art.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"png")
    workspace_artifact = profile_home / "workspace" / "Downloads" / "generated_art.png"

    response = router_mod._profile_scoped_media_response(
        f"created\nMEDIA:{source}",
        profile_home,
    )

    assert response == f"created\nMEDIA:{workspace_artifact.resolve()}"
    assert workspace_artifact.read_bytes() == b"png"


def test_profile_scoped_media_response_resolves_temp_path_to_direct_profile_home_file(tmp_path):
    """Tool-reported temp paths can resolve to same-name direct profile-home artifacts."""
    from hermes_multitenancy import router as router_mod

    profile_home = tmp_path / "profiles" / "owner"
    source = profile_home / "home" / "generated_art.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"png")
    workspace_artifact = profile_home / "workspace" / "Downloads" / "generated_art.png"

    response = router_mod._profile_scoped_media_response(
        "created\nMEDIA:/tmp/generated_art.png",
        profile_home,
    )

    assert response == f"created\nMEDIA:{workspace_artifact.resolve()}"
    assert workspace_artifact.read_bytes() == b"png"


def test_webui_profile_scoped_media_response_uses_workspace_alias_for_direct_home_file(tmp_path):
    """WebUI streamed MEDIA paths should be browser-downloadable workspace aliases."""
    from hermes_multitenancy import router as router_mod

    profile_home = tmp_path / "profiles" / "owner"
    source = profile_home / "home" / "generated_art.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"png")
    workspace_artifact = profile_home / "workspace" / "Downloads" / "generated_art.png"

    response = router_mod._webui_profile_scoped_media_response(
        f"created\nMEDIA:{source}",
        profile_home,
    )

    assert response == "created\nMEDIA:/workspace/Downloads/generated_art.png"
    assert workspace_artifact.read_bytes() == b"png"


def test_webui_profile_scoped_media_response_maps_workspace_absolute_to_alias(tmp_path):
    """Existing workspace artifacts are exposed to WebUI without server absolute paths."""
    from hermes_multitenancy import router as router_mod

    profile_home = tmp_path / "profiles" / "owner"
    source = profile_home / "workspace" / "Downloads" / "report.md"
    source.parent.mkdir(parents=True)
    source.write_text("# ok", encoding="utf-8")

    response = router_mod._webui_profile_scoped_media_response(
        f"done\nMEDIA:{source}",
        profile_home,
    )

    assert response == "done\nMEDIA:/workspace/Downloads/report.md"


def test_profile_scoped_media_response_does_not_publish_nested_profile_home_dotdir(tmp_path):
    """Only direct profile-home files are treated as generated artifacts."""
    from hermes_multitenancy import router as router_mod

    profile_home = tmp_path / "profiles" / "owner"
    source = profile_home / "home" / ".lark-cli" / "state.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"png")

    response = router_mod._profile_scoped_media_response(
        f"created\nMEDIA:{source}",
        profile_home,
    )

    assert response == f"created\nMEDIA:{source.resolve()}"
    assert not (profile_home / "workspace" / "Downloads" / "state.png").exists()


def test_profile_scoped_media_response_maps_sandbox_workspace_path(tmp_path):
    """Sandbox /workspace MEDIA paths map back to the routed profile workspace."""
    from hermes_multitenancy import router as router_mod

    profile_home = tmp_path / "profiles" / "owner"
    report = profile_home / "workspace" / "reports" / "summary.md"
    report.parent.mkdir(parents=True)
    report.write_text("ok", encoding="utf-8")

    response = router_mod._profile_scoped_media_response(
        "created\nMEDIA:/workspace/reports/summary.md",
        profile_home,
    )

    assert response == f"created\nMEDIA:{report.resolve()}"


def test_materialize_response_artifact_json_writes_workspace_file(tmp_path):
    from hermes_multitenancy import router as router_mod

    response = """
测试标记：OUT_MD
```hermes-artifact-json
{"path":"/workspace/Downloads/out.md","format":"md","content":"# title\\n\\nOUT_MARKER\\n"}
```
MEDIA:/workspace/Downloads/out.md
"""

    materialized = router_mod._materialize_response_artifacts(response, tmp_path)

    written = tmp_path / "workspace" / "Downloads" / "out.md"
    assert written.read_text(encoding="utf-8") == "# title\n\nOUT_MARKER\n"
    scoped = router_mod._profile_scoped_media_response(materialized, tmp_path)
    assert f"MEDIA:{written.resolve(strict=False)}" in scoped


def test_materialize_response_artifact_json_defaults_to_downloads_and_appends_media(tmp_path):
    from hermes_multitenancy import router as router_mod

    response = """
测试标记：OUT_MD
```hermes-artifact-json
{"filename":"out.md","format":"md","content":"# title\\n\\nOUT_MARKER\\n"}
```
"""

    materialized = router_mod._materialize_response_artifacts(response, tmp_path)

    written = tmp_path / "workspace" / "Downloads" / "out.md"
    assert written.read_text(encoding="utf-8") == "# title\n\nOUT_MARKER\n"
    assert "[[as_document]]" not in materialized
    assert "MEDIA:/workspace/Downloads/out.md" in materialized


def test_materialize_response_artifact_json_reuses_existing_markdown_source_for_marker_only_spec(tmp_path):
    """Marker-only markdown artifact specs must not overwrite a real generated source file."""
    from hermes_multitenancy import router as router_mod

    source = tmp_path / ".ai-docs" / "kep-prd-analysis" / "技术方案_数据中心分享接入动态模版_20260521.md"
    source.parent.mkdir(parents=True)
    source.write_text("# Real plan\n\nnot a placeholder\n", encoding="utf-8")

    response = """
done
```hermes-artifact-json
{"filename": "技术方案_数据中心分享接入动态模版_20260521.md", "format": "markdown", "marker": "TECH_PLAN"}
```
"""

    materialized = router_mod._materialize_response_artifacts(response, tmp_path)
    delivered = tmp_path / "workspace" / "Downloads" / source.name

    assert delivered.read_text(encoding="utf-8") == "# Real plan\n\nnot a placeholder\n"
    assert delivered.read_text(encoding="utf-8") != "TECH_PLAN"
    assert "MEDIA:/workspace/Downloads/技术方案_数据中心分享接入动态模版_20260521.md" in materialized


def test_materialize_response_artifact_json_ignores_markdown_as_document(tmp_path):
    from hermes_multitenancy import router as router_mod

    response = """
```hermes-artifact-json
{"filename":"out.markdown","format":"markdown","content":"# title\\n","as_document":true}
```
"""

    materialized = router_mod._materialize_response_artifacts(response, tmp_path)

    written = tmp_path / "workspace" / "Downloads" / "out.markdown"
    assert written.read_text(encoding="utf-8") == "# title\n"
    assert "[[as_document]]" not in materialized
    assert "MEDIA:/workspace/Downloads/out.markdown" in materialized


def test_materialize_response_artifact_json_can_request_image_document_delivery(tmp_path):
    from hermes_multitenancy import router as router_mod

    response = """
```hermes-artifact-json
{"filename":"out.png","format":"png","marker":"OUT_PNG","as_document":true}
```
"""

    materialized = router_mod._materialize_response_artifacts(response, tmp_path)

    assert (tmp_path / "workspace" / "Downloads" / "out.png").is_file()
    assert "[[as_document]]" in materialized
    assert "MEDIA:/workspace/Downloads/out.png" in materialized


def test_materialize_response_artifact_json_blocks_outside_workspace(tmp_path):
    from hermes_multitenancy import router as router_mod

    response = """
```hermes-artifact-json
{"path":"/workspace/../.env","format":"md","content":"SECRET=leak"}
```
MEDIA:/workspace/../.env
"""

    materialized = router_mod._materialize_response_artifacts(response, tmp_path)

    assert not (tmp_path / ".env").exists()
    assert materialized == response


def test_clean_stream_display_text_hides_artifact_json_blocks(tmp_path):
    from hermes_multitenancy import router as router_mod

    visible = router_mod._clean_stream_display_text(
        """测试标记：OUT
```hermes-artifact-json
{"path":"/workspace/Downloads/out.md","format":"md","content":"OUT_MARKER"}
``` 
[[as_document]]
MEDIA:/workspace/Downloads/out.md
正文仍然可见。""",
        tmp_path,
    )

    assert "hermes-artifact-json" not in visible
    assert "[[as_document]]" not in visible
    assert "MEDIA:" not in visible
    assert "正文仍然可见" in visible


def test_materialize_response_artifact_json_writes_structured_formats(tmp_path):
    from hermes_multitenancy import router as router_mod

    specs = [
        {"path": "/workspace/Downloads/out.json", "format": "json", "marker": "OUT_JSON", "data": {"marker": "OUT_JSON"}},
        {"path": "/workspace/Downloads/out.xlsx", "format": "xlsx", "marker": "OUT_XLSX", "rows": [["marker"], ["OUT_XLSX"]]},
        {"path": "/workspace/Downloads/out.docx", "format": "docx", "marker": "OUT_DOCX", "content": "OUT_DOCX"},
        {"path": "/workspace/Downloads/out.pdf", "format": "pdf", "marker": "OUT_PDF", "content": "OUT_PDF"},
        {"path": "/workspace/Downloads/out.png", "format": "png", "marker": "OUT_PNG"},
        {"path": "/workspace/Downloads/out.jpg", "format": "jpg", "marker": "OUT_JPG"},
    ]
    response = "\n".join(
        f"```hermes-artifact-json\n{json.dumps(spec)}\n```"
        for spec in specs
    )

    router_mod._materialize_response_artifacts(response, tmp_path)

    downloads = tmp_path / "workspace" / "Downloads"
    assert json.loads((downloads / "out.json").read_text(encoding="utf-8"))["marker"] == "OUT_JSON"
    assert "OUT_XLSX" in router_mod._extract_xlsx_text(downloads / "out.xlsx")
    assert "OUT_DOCX" in router_mod._extract_docx_text(downloads / "out.docx")
    assert "OUT_PDF" in router_mod._extract_pdf_text(downloads / "out.pdf")
    assert (downloads / "out.png").is_file()
    assert (downloads / "out.jpg").is_file()


def test_profile_scoped_media_response_blocks_temp_path_without_profile_artifact(tmp_path):
    """Unknown /tmp media stays blocked instead of being delivered from the host."""
    from hermes_multitenancy import router as router_mod

    profile_home = tmp_path / "profiles" / "owner"
    profile_home.mkdir(parents=True)

    response = router_mod._profile_scoped_media_response(
        "created\nMEDIA:/tmp/logo/logo.png",
        profile_home,
    )

    assert response == "created\n"


def test_materialize_inbound_media_copies_router_cache_into_profile(tmp_path):
    from hermes_multitenancy import router as router_mod

    router_cache = tmp_path / "profiles" / "multitenancy_router" / "cache" / "images"
    router_cache.mkdir(parents=True)
    source = router_cache / "img_abc.jpg"
    source.write_bytes(b"jpg")
    profile_home = tmp_path / "profiles" / "owner"
    profile_home.mkdir(parents=True)
    event = SimpleNamespace(
        text=f"/keep\\-record\n[Image] {source}",
        media_urls=[str(source)],
        media_types=["image/jpeg"],
    )

    router_mod._materialize_inbound_media_for_profile(event, profile_home)

    target = profile_home / "cache" / "images" / "img_abc.jpg"
    assert target.read_bytes() == b"jpg"
    assert event.media_urls == [str(target.resolve(strict=False))]
    assert str(source) not in event.text
    assert str(target.resolve(strict=False)) in event.text


def _write_minimal_xlsx(path: Path) -> None:
    files = {
        "[Content_Types].xml": """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>
</Types>""",
        "_rels/.rels": """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>""",
        "xl/workbook.xml": """<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="sheet1" sheetId="1" r:id="rId1"/></sheets>
</workbook>""",
        "xl/_rels/workbook.xml.rels": """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>""",
        "xl/sharedStrings.xml": """<?xml version="1.0" encoding="UTF-8"?>
<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="4" uniqueCount="4">
  <si><t>marker</t></si>
  <si><t>amount</t></si>
  <si><t>HERMES_MT_XLSX_MARKER</t></si>
  <si><t>42</t></si>
</sst>""",
        "xl/worksheets/sheet1.xml": """<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>
    <row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>
    <row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2" t="s"><v>3</v></c></row>
  </sheetData>
</worksheet>""",
    }
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)


def _write_inline_string_xlsx(path: Path) -> None:
    files = {
        "[Content_Types].xml": """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>""",
        "_rels/.rels": """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>""",
        "xl/workbook.xml": """<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="matrix" sheetId="1" r:id="rId1"/></sheets>
</workbook>""",
        "xl/_rels/workbook.xml.rels": """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>""",
        "xl/worksheets/sheet1.xml": """<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>
    <row r="1"><c r="A1" t="inlineStr"><is><t>marker</t></is></c><c r="B1" t="inlineStr"><is><t>amount</t></is></c></row>
    <row r="2"><c r="A2" t="inlineStr"><is><t>HERMES_MT_XLSX_INLINE_MARKER</t></is></c><c r="B2"><v>42</v></c></row>
  </sheetData>
</worksheet>""",
    }
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)


def _write_minimal_docx(path: Path) -> None:
    files = {
        "[Content_Types].xml": """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>""",
        "_rels/.rels": """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>""",
        "word/document.xml": """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>Hermes DOCX media matrix</w:t></w:r></w:p>
    <w:p><w:r><w:t>HERMES_MT_DOCX_MARKER</w:t></w:r></w:p>
  </w:body>
</w:document>""",
    }
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)


def _write_minimal_pdf(path: Path) -> None:
    stream = b"BT 72 720 Td (HERMES_MT_PDF_MARKER) Tj T* (Please summarize this PDF.) Tj ET"
    encoded = base64.a85encode(zlib.compress(stream), adobe=True)
    path.write_bytes(
        b"%PDF-1.3\n"
        b"1 0 obj << /Filter [ /ASCII85Decode /FlateDecode ] /Length "
        + str(len(encoded)).encode("ascii")
        + b" >>\nstream\n"
        + encoded
        + b"\nendstream\nendobj\n%%EOF\n"
    )


def test_local_file_enrichment_extracts_csv_and_xlsx(tmp_path):
    """Feishu document compatibility belongs in multitenancy, not Hermes-agent patches."""
    from hermes_multitenancy import router as router_mod

    csv_path = tmp_path / "table.csv"
    csv_path.write_text("marker,amount\nHERMES_MT_CSV_MARKER,7\n", encoding="utf-8")
    xlsx_path = tmp_path / "sheet.xlsx"
    _write_minimal_xlsx(xlsx_path)
    event = SimpleNamespace(
        text="",
        media_urls=[str(csv_path), str(xlsx_path)],
        media_types=["application/octet-stream", "application/octet-stream"],
    )

    enriched = router_mod._local_enrich_with_file_content(event)

    assert "[Content of table.csv]" in enriched
    assert "HERMES_MT_CSV_MARKER" in enriched
    assert "[Content of sheet.xlsx]" in enriched
    assert "HERMES_MT_XLSX_MARKER" in enriched


def test_local_file_enrichment_extracts_docx_and_pdf(tmp_path):
    from hermes_multitenancy import router as router_mod

    docx_path = tmp_path / "doc.docx"
    pdf_path = tmp_path / "doc.pdf"
    _write_minimal_docx(docx_path)
    _write_minimal_pdf(pdf_path)
    event = SimpleNamespace(
        text="",
        media_urls=[str(docx_path), str(pdf_path)],
        media_types=["application/octet-stream", "application/octet-stream"],
    )

    enriched = router_mod._local_enrich_with_file_content(event)

    assert "[Content of doc.docx]" in enriched
    assert "HERMES_MT_DOCX_MARKER" in enriched
    assert "[Content of doc.pdf]" in enriched
    assert "HERMES_MT_PDF_MARKER" in enriched


def test_local_file_enrichment_adds_doc_fallback_when_native_preview_is_lossy(tmp_path):
    from hermes_multitenancy import router as router_mod

    docx_path = tmp_path / "lossy.docx"
    _write_minimal_docx(docx_path)
    event = SimpleNamespace(
        text="",
        media_urls=[str(docx_path)],
        media_types=["application/octet-stream"],
    )

    enriched = router_mod._local_enrich_with_file_content(
        event,
        existing_text="[The user sent a document: 'lossy.docx'. Ask the user what they'd like you to do with it.]",
    )

    assert enriched is not None
    assert "HERMES_MT_DOCX_MARKER" in enriched


def test_local_file_enrichment_extracts_xlsx_inline_strings(tmp_path):
    """Real Feishu/openpyxl sheets may store strings inline, not in sharedStrings.xml."""
    from hermes_multitenancy import router as router_mod

    xlsx_path = tmp_path / "inline.xlsx"
    _write_inline_string_xlsx(xlsx_path)
    event = SimpleNamespace(
        text="",
        media_urls=[str(xlsx_path)],
        media_types=["application/octet-stream"],
    )

    enriched = router_mod._local_enrich_with_file_content(event)

    assert "[Content of inline.xlsx]" in enriched
    assert "marker\tamount" in enriched
    assert "HERMES_MT_XLSX_INLINE_MARKER\t42" in enriched


def test_local_file_enrichment_adds_xlsx_fallback_when_native_preview_is_lossy(tmp_path):
    """If Hermes' native xlsx preview drops strings, multitenancy appends its richer preview."""
    from hermes_multitenancy import router as router_mod

    xlsx_path = tmp_path / "lossy.xlsx"
    _write_inline_string_xlsx(xlsx_path)
    event = SimpleNamespace(
        text="",
        media_urls=[str(xlsx_path)],
        media_types=["application/octet-stream"],
    )

    enriched = router_mod._local_enrich_with_file_content(
        event,
        existing_text="[Content of lossy.xlsx]:\n[matrix]\n\t\t42",
    )

    assert enriched is not None
    assert "HERMES_MT_XLSX_INLINE_MARKER\t42" in enriched


def test_local_file_enrichment_handles_missing_media_types_and_skips_large_files(tmp_path):
    """Attachment fallback should cover all media URLs and avoid parsing large payloads."""
    from hermes_multitenancy import router as router_mod

    oversized = tmp_path / "huge.csv"
    oversized.write_bytes(b"x" * (router_mod._MAX_LOCAL_ENRICH_FILE_BYTES + 1))
    xlsx_path = tmp_path / "sheet.xlsx"
    _write_minimal_xlsx(xlsx_path)
    event = SimpleNamespace(
        text="",
        media_urls=[str(oversized), str(xlsx_path)],
        media_types=["application/octet-stream"],
    )

    enriched = router_mod._local_enrich_with_file_content(event)

    assert "huge.csv" not in enriched
    assert "[Content of sheet.xlsx]" in enriched
    assert "HERMES_MT_XLSX_MARKER" in enriched


def test_event_has_image_media_detects_gateway_photo_enum_shape(tmp_path):
    """Feishu photo events may carry MessageType.PHOTO with blank media_type."""
    from hermes_multitenancy import router as router_mod

    event = SimpleNamespace(
        message_type=SimpleNamespace(name="PHOTO", value="photo"),
        media_urls=[str(tmp_path / "cache" / "images" / "img_abc")],
        media_types=[""],
    )

    assert router_mod._event_has_image_media(event)


def test_event_has_image_media_detects_gateway_image_cache_path(tmp_path):
    """Feishu image cache paths can be extensionless with blank media_type."""
    from hermes_multitenancy import router as router_mod

    event = SimpleNamespace(
        media_urls=[str(tmp_path / "cache" / "images" / "img_abc")],
        media_types=[""],
    )

    assert router_mod._event_has_image_media(event)


@pytest.mark.asyncio
async def test_enrich_via_hermes_pipeline_blocks_image_preprocessing_when_configured(monkeypatch, tmp_path):
    """Current Feishu UAT should not hang on unavailable vision credentials."""
    from hermes_multitenancy import router as router_mod

    monkeypatch.setenv("HERMES_MULTITENANCY_IMAGE_PREP_STRATEGY", "blocked")
    event = SimpleNamespace(
        text="",
        media_urls=[str(tmp_path / "cache" / "images" / "img_abc")],
        media_types=[""],
        source=SimpleNamespace(),
    )

    class Gateway:
        async def _prepare_inbound_message_text(self, *, event, source, history):
            raise AssertionError("default image strategy should not call gateway vision preprocessing")

    enriched = await router_mod._enrich_via_hermes_pipeline(event, Gateway())

    assert "something went wrong when I tried to look at it" in enriched
    assert "vision_analyze" in enriched


@pytest.mark.asyncio
async def test_enrich_via_hermes_pipeline_times_out_image_preprocessing(monkeypatch, tmp_path):
    """Slow/broken image vision preprocessing should surface a bounded unsupported note."""
    from hermes_multitenancy import router as router_mod

    monkeypatch.setenv("HERMES_MULTITENANCY_IMAGE_PREP_STRATEGY", "gateway")
    monkeypatch.setenv("HERMES_MULTITENANCY_IMAGE_PREP_TIMEOUT_S", "0.01")
    event = SimpleNamespace(
        text="",
        media_urls=[str(tmp_path / "inbound.jpg")],
        media_types=["image/jpeg"],
        source=SimpleNamespace(),
    )

    class Gateway:
        async def _prepare_inbound_message_text(self, *, event, source, history):
            await asyncio.sleep(1)
            return "too late"

    enriched = await router_mod._enrich_via_hermes_pipeline(event, Gateway())

    assert "vision auto-analysis timed out" in enriched
    assert "vision_analyze" in enriched


@pytest.mark.asyncio
async def test_hook_returns_skip_action():
    """callback returns {action: skip} so Hermes main flow halts."""
    from hermes_multitenancy import on_pre_gateway_dispatch

    event = _build_event()
    gateway = SimpleNamespace(adapters={})  # no adapter — handle_async will no-op

    result = on_pre_gateway_dispatch(event=event, gateway=gateway, session_store=None)

    assert isinstance(result, dict)
    assert result.get("action") == "skip"
    assert "reason" in result
    # Drain any tasks that were scheduled so pytest-asyncio doesn't warn
    await asyncio.sleep(0)


def test_startup_watch_starts_cron_worker_when_adapters_ready(monkeypatch):
    """The plugin startup watcher initializes cron without Feishu inbound."""
    from hermes_multitenancy import cron_worker

    calls = []
    monkeypatch.setattr(
        cron_worker,
        "ensure_cron_worker_started",
        lambda gateway: calls.append(gateway),
    )
    gateway = SimpleNamespace(adapters={"feishu": object()})

    asyncio.run(cron_worker._start_worker_when_adapters_ready(gateway, attempts=1))

    assert calls == [gateway]


def test_cron_delivery_patch_resolves_owner_open_id(monkeypatch):
    """Bare deliver=feishu can target the WebUI owner's Feishu open_id."""
    import sys
    import types

    from hermes_multitenancy import cron_worker

    cron_pkg = types.ModuleType("cron")
    scheduler = types.ModuleType("cron.scheduler")

    def original_resolver(_job, _deliver_value):
        return None

    scheduler._resolve_single_delivery_target = original_resolver
    cron_pkg.scheduler = scheduler
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.scheduler", scheduler)

    cron_worker._patch_scheduler_owner_open_id_delivery()

    target = scheduler._resolve_single_delivery_target(
        {"deliver": "feishu", "owner_open_id": "ou_test_owner"},
        "feishu",
    )

    assert target == {
        "platform": "feishu",
        "chat_id": "ou_test_owner",
        "thread_id": None,
    }


def test_cron_run_broker_patch_submits_cron_run_request(monkeypatch, tmp_path):
    """Cron run patch should execute due jobs through RunBroker by default."""
    import sys
    import types

    from hermes_multitenancy import cron_worker

    cron_pkg = types.ModuleType("cron")
    scheduler = types.ModuleType("cron.scheduler")
    seen = []

    def original_run_job(_job):
        raise AssertionError("legacy cron run_job should not execute")

    def build_prompt(job, prerun_script=None):
        return f"cron prompt: {job['prompt']}"

    scheduler.run_job = original_run_job
    scheduler._build_job_prompt = build_prompt
    cron_pkg.scheduler = scheduler
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.scheduler", scheduler)
    monkeypatch.delenv("HERMES_MULTITENANCY_CRON_RUN_BROKER", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profiles" / "owner"))

    class FakeBroker:
        def __init__(self, dispatch_agent=None, **_kwargs):
            self.dispatch_agent = dispatch_agent

        async def run(self, request):
            seen.append(request)
            return types.SimpleNamespace(content="broker cron ok", duplicate=False, run_id="run_cron_1")

    monkeypatch.setattr(cron_worker, "RunBroker", FakeBroker)
    cron_worker._patch_cron_run_broker()

    success, output, final_response, error = scheduler.run_job({
        "id": "job123",
        "name": "Daily summary",
        "prompt": "summarize",
        "deliver": "feishu",
        "owner_open_id": "ou_owner",
        "owner_profile": "owner",
        "model": "gpt-5.4",
        "provider": "openai",
    })

    assert success is True
    assert "broker cron ok" in output
    assert final_response == "broker cron ok"
    assert error is None
    assert len(seen) == 1
    request = seen[0]
    assert request.channel == "cron"
    assert request.profile_name == "owner"
    assert request.user_key == "ou_owner"
    assert request.content.startswith("cron prompt: summarize")
    assert "Do not respond with [SILENT]" in request.content
    assert request.session_id == "cron:job123"
    assert request.credential_subject == "ou_owner"
    assert request.requires_host_tools is True
    assert request.metadata["job_id"] == "job123"
    assert request.metadata["model"] == "gpt-5.4"
    assert request.metadata["provider"] == "openai"


def test_cron_run_broker_patch_makes_silent_response_visible(monkeypatch, tmp_path):
    """Cron delivery must stay visible even when upstream prompt asks for [SILENT]."""
    import sys
    import types

    from hermes_multitenancy import cron_worker

    cron_pkg = types.ModuleType("cron")
    scheduler = types.ModuleType("cron.scheduler")

    def original_run_job(_job):
        raise AssertionError("legacy cron run_job should not execute")

    scheduler.run_job = original_run_job
    scheduler._build_job_prompt = lambda job, prerun_script=None: f"cron prompt: {job['prompt']}"
    cron_pkg.scheduler = scheduler
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.scheduler", scheduler)
    monkeypatch.delenv("HERMES_MULTITENANCY_CRON_RUN_BROKER", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profiles" / "owner"))

    class FakeBroker:
        def __init__(self, **_kwargs):
            pass

        async def run(self, request):
            assert "Do not respond with [SILENT]" in request.content
            return types.SimpleNamespace(content="  [silent].  ", duplicate=False, run_id="run_cron_1")

    monkeypatch.setattr(cron_worker, "RunBroker", FakeBroker)
    cron_worker._patch_cron_run_broker()

    success, output, final_response, error = scheduler.run_job({
        "id": "job123",
        "name": "Daily summary",
        "prompt": "summarize",
        "deliver": "feishu",
        "owner_open_id": "ou_owner",
        "owner_profile": "owner",
    })

    assert success is True
    assert error is None
    assert "[SILENT]" not in final_response
    assert "本次没有发现需要提醒的事项" in final_response
    assert "**Run Path:** RunBroker" in output


def test_profile_native_cron_tick_guard_yields_to_router(monkeypatch):
    """Non-router profile gateways must not execute cron jobs natively."""
    import sys
    import types

    from hermes_multitenancy import cron_worker

    cron_pkg = types.ModuleType("cron")
    scheduler = types.ModuleType("cron.scheduler")
    calls = []

    def original_tick(*_args, **_kwargs):
        calls.append("tick")
        return 1

    scheduler.tick = original_tick
    cron_pkg.scheduler = scheduler
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.scheduler", scheduler)
    monkeypatch.setenv("HERMES_HOME", "/tmp/hermes/profiles/owner")
    monkeypatch.delenv("HERMES_MULTITENANCY_PROFILE_NATIVE_CRON", raising=False)

    cron_worker.install_profile_native_cron_guard()

    assert scheduler.tick(verbose=False) == 0
    assert calls == []


def test_profile_native_cron_tick_escape_logs_warning(monkeypatch, caplog):
    """Emergency native cron escape should be auditable in logs."""
    import sys
    import types
    import logging

    from hermes_multitenancy import cron_worker

    cron_pkg = types.ModuleType("cron")
    scheduler = types.ModuleType("cron.scheduler")
    calls = []

    def original_tick(*_args, **_kwargs):
        calls.append("tick")
        return 1

    scheduler.tick = original_tick
    cron_pkg.scheduler = scheduler
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.scheduler", scheduler)
    monkeypatch.setenv("HERMES_HOME", "/tmp/hermes/profiles/owner")
    monkeypatch.setenv("HERMES_MULTITENANCY_PROFILE_NATIVE_CRON", "1")

    cron_worker.install_profile_native_cron_guard()

    with caplog.at_level(logging.WARNING):
        assert scheduler.tick(verbose=False) == 1

    assert calls == ["tick"]
    assert "profile native cron tick escape enabled for owner" in caplog.text


def test_cron_run_broker_trigger_http_error_is_loud(monkeypatch, tmp_path):
    """Manual trigger forwarder must raise a visible error on broker auth/config failure."""
    import io
    from urllib.error import HTTPError

    from hermes_multitenancy import cron_worker

    profile_home = tmp_path / ".hermes" / "profiles" / "owner"
    profile_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", "broker-secret")

    def fail_urlopen(_request, timeout=15):
        assert timeout == 15
        raise HTTPError(
            url="http://127.0.0.1:8766/api/run-broker/jobs/job123/run",
            code=401,
            msg="Unauthorized",
            hdrs={},
            fp=io.BytesIO(b'{"error":"unauthorized"}'),
        )

    monkeypatch.setattr(cron_worker.urllib_request, "urlopen", fail_urlopen)

    with pytest.raises(RuntimeError, match='RunBroker cron trigger failed \\(401\\): {"error":"unauthorized"}'):
        cron_worker.trigger_profile_cron_job_via_run_broker(
            job_id="job123",
            profile_home=profile_home,
            owner_open_id="ou_owner",
        )


def test_cron_run_request_rejects_missing_owner_and_router_profile(tmp_path):
    """Cron bridge must not fall back to the shared/router profile identity."""
    from hermes_multitenancy import cron_worker

    profile_home = tmp_path / ".hermes" / "profiles" / "owner"
    router_home = tmp_path / ".hermes" / "profiles" / "multitenancy_router"

    with pytest.raises(ValueError, match="owner_open_id"):
        cron_worker._build_cron_run_request(
            {
                "id": "job123",
                "name": "bad cron",
                "prompt": "should not run",
                "deliver": "feishu",
            },
            profile_home=profile_home,
            prompt="should not run",
        )
    with pytest.raises(ValueError, match="multitenancy_router"):
        cron_worker._build_cron_run_request(
            {
                "id": "job124",
                "name": "router cron",
                "prompt": "should not run",
                "owner_open_id": "ou_owner",
                "owner_profile": "multitenancy_router",
                "deliver": "feishu",
            },
            profile_home=router_home,
            prompt="should not run",
        )


def test_cron_owner_context_backfill_repairs_legacy_feishu_job(tmp_path):
    """Legacy Feishu cron jobs created before owner fields are repaired without running."""
    from hermes_multitenancy import cron_worker
    from hermes_multitenancy.routing import RoutingTable

    shared_home = tmp_path / ".hermes"
    profile_home = shared_home / "profiles" / "owner"
    cron_dir = profile_home / "cron"
    cron_dir.mkdir(parents=True)
    table = RoutingTable(shared_home / "multitenancy.db")
    table.upsert(user_id="owner", profile_name="owner", open_id="ou_owner", provenance="sync")
    jobs_file = cron_dir / "jobs.json"
    jobs_file.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "job123",
                        "name": "IT双周会前一天提醒",
                        "prompt": "summarize",
                        "deliver": "feishu",
                        "origin": {"platform": "feishu", "chat_id": "oc_dm"},
                        "next_run_at": "2026-05-28T10:00:00+08:00",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = cron_worker.backfill_cron_owner_context_for_profile(profile_home)
    repaired = json.loads(jobs_file.read_text(encoding="utf-8"))["jobs"][0]

    assert result["checked"] == 1
    assert result["updated"] == 1
    assert repaired["owner_open_id"] == "ou_owner"
    assert repaired["owner_profile"] == "owner"
    assert repaired["prompt"] == "summarize"
    assert repaired["next_run_at"] == "2026-05-28T10:00:00+08:00"

    plan = cron_worker.plan_cron_bridge_run(repaired, profile_home=profile_home, due=True)
    assert plan["would_execute"] is True
    assert plan["problems"] == []
    assert plan["deliver_target"] == {"platform": "feishu", "chat_id": "ou_owner", "thread_id": None}


def test_cron_plan_infers_owner_without_persisting_when_route_exists(tmp_path):
    """RunBroker planning can repair old in-memory job dicts before JSON backfill runs."""
    from hermes_multitenancy import cron_worker
    from hermes_multitenancy.routing import RoutingTable

    shared_home = tmp_path / ".hermes"
    profile_home = shared_home / "profiles" / "owner"
    profile_home.mkdir(parents=True)
    table = RoutingTable(shared_home / "multitenancy.db")
    table.upsert(user_id="owner", profile_name="owner", open_id="ou_owner", provenance="sync")

    plan = cron_worker.plan_cron_bridge_run(
        {
            "id": "job123",
            "name": "legacy",
            "prompt": "summarize",
            "deliver": "feishu",
        },
        profile_home=profile_home,
        due=True,
        shadow=True,
    )

    assert plan["would_execute"] is True
    assert plan["user_key"] == "ou_owner"
    assert plan["credential_subject"] == "ou_owner"
    assert plan["deliver_target"] == {"platform": "feishu", "chat_id": "ou_owner", "thread_id": None}
    assert plan["problems"] == []


def test_cron_bridge_shadow_plan_is_secret_free_and_non_executing(tmp_path):
    """Shadow mode should explain the cron route without dispatching or leaking secrets."""
    from hermes_multitenancy import cron_worker

    profile_home = tmp_path / ".hermes" / "profiles" / "owner"
    plan = cron_worker.plan_cron_bridge_run(
        {
            "id": "job123",
            "name": "Daily summary",
            "prompt": "summarize",
            "deliver": "feishu",
            "owner_open_id": "ou_owner",
            "owner_profile": "owner",
            "next_run_at": "2026-05-20T09:30:00+08:00",
            "enabled": True,
            "state": "scheduled",
            "env": {"OPENAI_API_KEY": "sk-should-not-leak"},
        },
        profile_home=profile_home,
        due=True,
        shadow=True,
    )

    assert plan["mode"] == "shadow"
    assert plan["will_execute"] is False
    assert plan["would_execute"] is True
    assert plan["profile_name"] == "owner"
    assert plan["profile_home"] == str(profile_home)
    assert plan["user_key"] == "ou_owner"
    assert plan["credential_subject"] == "ou_owner"
    assert plan["deliver_target"] == {"platform": "feishu", "chat_id": "ou_owner", "thread_id": None}
    assert plan["next_run_at"] == "2026-05-20T09:30:00+08:00"
    assert plan["secret_free"] is True
    assert "should-not-leak" not in json.dumps(plan)


def test_feishu_response_message_id_handles_sdk_and_dict_shapes():
    from hermes_multitenancy import cron_worker

    assert cron_worker._feishu_response_message_id(SimpleNamespace(data=SimpleNamespace(message_id="om_sdk"))) == "om_sdk"
    assert cron_worker._feishu_response_message_id({"message_id": "om_dict"}) == "om_dict"
    assert cron_worker._feishu_response_message_id({"message": {"message_id": "om_nested"}}) == "om_nested"
    assert cron_worker._feishu_response_message_id(object()) is None


def test_cron_delivery_mirror_persists_owner_context(tmp_path, monkeypatch):
    """Successful cron delivery is remembered for the owner's next Feishu turn."""
    from hermes_multitenancy import cron_worker
    from hermes_multitenancy.router import override_session_store
    from hermes_multitenancy.sessions import SessionStore

    store = SessionStore(tmp_path / "multitenancy.db")
    override_session_store(store)
    try:
        cron_worker._mirror_cron_delivery_to_owner(
            {
                "id": "job123",
                "name": "Daily summary",
                "owner_profile": "owner",
                "owner_open_id": "ou_test_owner",
            },
            "summary content",
        )

        messages = store.load_recent("owner", "ou_test_owner", 5)
        assert messages == [{
            "role": "assistant",
            "content": (
                "[Scheduled task delivery]\n"
                "Task: Daily summary\n"
                "Job ID: job123\n\n"
                "summary content"
            ),
        }]
    finally:
        override_session_store(None)
        store.close()


def test_cron_delivery_patch_uses_live_feishu_adapter_when_platform_config_missing(monkeypatch):
    """Multitenancy Feishu cron delivery must use the live adapter when native config lacks Feishu."""
    import sys
    import types

    from hermes_multitenancy import cron_worker

    cron_pkg = types.ModuleType("cron")
    scheduler = types.ModuleType("cron.scheduler")
    sent = []

    def original_deliver(_job, _content, adapters=None, loop=None):
        return "platform 'feishu' not configured/enabled"

    def resolve_targets(_job):
        return [{"platform": "feishu", "chat_id": "oc_target", "thread_id": None}]

    scheduler._deliver_result = original_deliver
    scheduler._resolve_delivery_targets = resolve_targets
    cron_pkg.scheduler = scheduler
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.scheduler", scheduler)

    class Adapter:
        async def send(self, chat_id, text, metadata=None):
            sent.append((chat_id, text, metadata))
            return types.SimpleNamespace(success=True)

    def run_now(coro, _loop):
        result = asyncio.run(coro)
        return types.SimpleNamespace(result=lambda timeout=None: result, cancel=lambda: None)

    monkeypatch.setattr("agent.async_utils.safe_schedule_threadsafe", run_now)
    cron_worker._patch_cron_delivery_mirror()

    error = scheduler._deliver_result(
        {"id": "job123", "name": "IT reminder", "owner_open_id": "ou_owner", "owner_profile": "owner"},
        "cron body",
        adapters={"feishu": Adapter()},
        loop=types.SimpleNamespace(is_running=lambda: True),
    )

    assert error is None
    assert len(sent) == 1
    assert sent[0][0] == "oc_target"
    assert "Cronjob Response: IT reminder" in sent[0][1]
    assert "cron body" in sent[0][1]


def test_cron_delivery_patch_does_not_double_send_after_native_success(monkeypatch):
    """Native delivery success must stay the only send path."""
    import sys
    import types

    from hermes_multitenancy import cron_worker

    cron_pkg = types.ModuleType("cron")
    scheduler = types.ModuleType("cron.scheduler")
    sent = []

    scheduler._deliver_result = lambda _job, _content, adapters=None, loop=None: None
    scheduler._resolve_delivery_targets = lambda _job: [{"platform": "feishu", "chat_id": "oc_target"}]
    cron_pkg.scheduler = scheduler
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.scheduler", scheduler)

    class Adapter:
        async def send(self, chat_id, text, metadata=None):
            sent.append((chat_id, text, metadata))
            return types.SimpleNamespace(success=True)

    cron_worker._patch_cron_delivery_mirror()

    error = scheduler._deliver_result(
        {"id": "job123", "name": "Daily digest", "owner_open_id": "ou_owner", "owner_profile": "owner"},
        "cron body",
        adapters={"feishu": Adapter()},
        loop=types.SimpleNamespace(is_running=lambda: True),
    )

    assert error is None
    assert sent == []


def test_cron_delivery_patch_preserves_error_without_live_adapter(monkeypatch):
    """Missing live adapter is not hidden as a successful cron delivery."""
    import sys
    import types

    from hermes_multitenancy import cron_worker

    cron_pkg = types.ModuleType("cron")
    scheduler = types.ModuleType("cron.scheduler")
    original_error = "platform 'feishu' not configured/enabled"
    scheduler._deliver_result = lambda _job, _content, adapters=None, loop=None: original_error
    scheduler._resolve_delivery_targets = lambda _job: [{"platform": "feishu", "chat_id": "oc_target"}]
    cron_pkg.scheduler = scheduler
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.scheduler", scheduler)

    cron_worker._patch_cron_delivery_mirror()

    error = scheduler._deliver_result(
        {"id": "job123", "name": "Daily digest", "owner_open_id": "ou_owner", "owner_profile": "owner"},
        "cron body",
        adapters={},
        loop=types.SimpleNamespace(is_running=lambda: True),
    )

    assert original_error in error
    assert "feishu live adapter unavailable" in error


def test_cron_delivery_patch_filters_thread_id_for_feishu_dm(monkeypatch):
    """Fallback must not pass stale thread_id metadata to Feishu open_id DMs."""
    import sys
    import types

    from hermes_multitenancy import cron_worker

    cron_pkg = types.ModuleType("cron")
    scheduler = types.ModuleType("cron.scheduler")
    sent = []

    scheduler._deliver_result = lambda _job, _content, adapters=None, loop=None: "platform 'feishu' not configured/enabled"
    scheduler._resolve_delivery_targets = lambda _job: [{"platform": "feishu", "chat_id": "ou_owner", "thread_id": "omt_stale"}]
    cron_pkg.scheduler = scheduler
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.scheduler", scheduler)

    class Adapter:
        async def send(self, chat_id, text, metadata=None):
            sent.append((chat_id, metadata))
            return types.SimpleNamespace(success=True)

    def run_now(coro, _loop):
        result = asyncio.run(coro)
        return types.SimpleNamespace(result=lambda timeout=None: result, cancel=lambda: None)

    monkeypatch.setattr("agent.async_utils.safe_schedule_threadsafe", run_now)
    cron_worker._patch_cron_delivery_mirror()

    error = scheduler._deliver_result(
        {"id": "job123", "name": "Daily digest", "owner_open_id": "ou_owner", "owner_profile": "owner"},
        "cron body",
        adapters={"feishu": Adapter()},
        loop=types.SimpleNamespace(is_running=lambda: True),
    )

    assert error is None
    assert sent == [("ou_owner", None)]


def test_cron_delivery_patch_handles_media_branch_without_name_error(monkeypatch):
    """A successful media fallback must not fail because of local bookkeeping."""
    import sys
    import types

    from hermes_multitenancy import cron_worker

    cron_pkg = types.ModuleType("cron")
    scheduler = types.ModuleType("cron.scheduler")
    sent = []
    media_sent = []

    scheduler._deliver_result = lambda _job, _content, adapters=None, loop=None: "platform 'feishu' not configured/enabled"
    scheduler._resolve_delivery_targets = lambda _job: [{"platform": "feishu", "chat_id": "oc_target", "thread_id": None}]
    cron_pkg.scheduler = scheduler
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.scheduler", scheduler)
    monkeypatch.setattr(cron_worker, "_cron_delivery_payload_for_adapter", lambda _job, _content: ("text body", [("/tmp/a.png", False)]))
    monkeypatch.setattr(
        cron_worker,
        "_send_media_files_via_live_adapter",
        lambda _adapter, chat_id, media_files, _metadata, _loop, _job: media_sent.append((chat_id, media_files)) or None,
    )

    class Adapter:
        async def send(self, chat_id, text, metadata=None):
            sent.append((chat_id, text, metadata))
            return types.SimpleNamespace(success=True)

    def run_now(coro, _loop):
        result = asyncio.run(coro)
        return types.SimpleNamespace(result=lambda timeout=None: result, cancel=lambda: None)

    monkeypatch.setattr("agent.async_utils.safe_schedule_threadsafe", run_now)
    cron_worker._patch_cron_delivery_mirror()

    error = scheduler._deliver_result(
        {"id": "job123", "name": "Media digest", "owner_open_id": "ou_owner", "owner_profile": "owner"},
        "cron body",
        adapters={"feishu": Adapter()},
        loop=types.SimpleNamespace(is_running=lambda: True),
    )

    assert error is None
    assert sent == [("oc_target", "text body", None)]
    assert media_sent == [("oc_target", [("/tmp/a.png", False)])]


def test_cron_worker_reads_active_profiles_from_routing_db(tmp_path):
    """Inactive historical profiles should not be scanned for cron jobs."""
    import sqlite3

    from hermes_multitenancy import cron_worker

    profiles_root = tmp_path / "profiles"
    profiles_root.mkdir()
    db_path = tmp_path / "multitenancy.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE multitenancy_routing (profile_name TEXT, active INTEGER)"
        )
        conn.execute(
            "INSERT INTO multitenancy_routing(profile_name, active) VALUES (?, ?)",
            ("owner", 1),
        )
        conn.execute(
            "INSERT INTO multitenancy_routing(profile_name, active) VALUES (?, ?)",
            ("feishu_ou_old", 0),
        )

    assert cron_worker._active_cron_profiles(profiles_root) == {"owner"}


@pytest.mark.asyncio
async def test_hook_schedules_background_task():
    """callback creates a background asyncio task (fire-and-forget)."""
    from hermes_multitenancy import on_pre_gateway_dispatch
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes
    from pathlib import Path
    import tempfile

    clear_spike_routes()


    with tempfile.TemporaryDirectory() as tmp:
        add_spike_route("ou_test_bg", Path(tmp))

        event = _build_event(user_id="ou_test_bg")

        # Track adapter calls so we can verify the task ran
        send_typing_calls = []
        send_calls = []

        class MockAdapter:
            async def send_typing(self, chat_id):
                send_typing_calls.append(chat_id)

            async def send(self, chat_id, content, *, reply_to=None, metadata=None):
                send_calls.append((chat_id, content))

        gateway = SimpleNamespace(adapters={"feishu": MockAdapter()})

        # Count tasks before
        before = len(asyncio.all_tasks())

        result = on_pre_gateway_dispatch(event=event, gateway=gateway, session_store=None)
        # Skip is returned synchronously
        assert result["action"] == "skip"

        # A new task was scheduled
        after = len(asyncio.all_tasks())
        assert after > before, f"expected >1 task scheduled (before={before}, after={after})"

        # Let the task run
        await asyncio.sleep(0.05)

        # Adapter should have received both calls (full loop runs)
        assert len(send_typing_calls) == 1
        assert send_typing_calls[0] == "chat-123"
        assert len(send_calls) == 1
        assert send_calls[0][0] == "chat-123"

    clear_spike_routes()


@pytest.mark.asyncio
async def test_first_billing_identity_failure_is_visible_and_retryable(monkeypatch, tmp_path):
    from hermes_multitenancy import billing_identity
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.run_broker import RunRejected
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes

    clear_spike_routes()
    add_spike_route("ou_billing_retry", tmp_path)
    sent = []
    admitted = []

    async def reject(_request, **_kwargs):
        raise RunRejected("LiteLLM management API is unavailable")

    async def must_not_enrich(*_args, **_kwargs):
        raise AssertionError("model-backed enrichment must not start")

    class Adapter:
        async def send(self, chat_id, text):
            sent.append((chat_id, text))

    monkeypatch.setattr(billing_identity, "prepare_billing_request", reject)
    monkeypatch.setattr(
        router_mod,
        "_mark_run_request_seen",
        lambda request: admitted.append(request) or True,
    )
    monkeypatch.setattr(
        router_mod,
        "_call_enrich_via_hermes_pipeline",
        must_not_enrich,
    )

    event = _build_event(text="hello", user_id="ou_billing_retry")
    event.message_id = "om_billing_retry"
    await router_mod.handle_async(
        event=event,
        gateway=SimpleNamespace(adapters={"feishu": Adapter()}),
    )

    assert sent == [("chat-123", "当前无法确认员工计费身份，请稍后重试。")]
    assert admitted == []
    clear_spike_routes()


def test_incident_replay_dispatches_twelve_noncohort_events_without_billing_error(
    monkeypatch,
    tmp_path,
):
    from hermes_multitenancy import billing_identity
    from hermes_multitenancy import router as router_mod

    class Routing:
        def __init__(self):
            self.rows = {
                f"employee_{index}": SimpleNamespace(
                    user_id=f"employee_{index}",
                    profile_name=f"profile_{index}",
                    open_id=f"ou_fixture_{index}",
                    active=True,
                    kind="user",
                    provenance="sync",
                )
                for index in range(6)
            }

        def lookup_by_user_id(self, user_id):
            return self.rows.get(user_id)

        def resolve_owner_root(self, open_id):
            return next(
                (row for row in self.rows.values() if row.open_id == open_id),
                None,
            )

        def lookup_by_open_id(self, open_id):
            return self.resolve_owner_root(open_id)

        def lookup_by_profile_name(self, profile_name):
            row = next(
                (
                    value
                    for value in self.rows.values()
                    if value.profile_name == profile_name
                ),
                None,
            )
            return (
                SimpleNamespace(owner_open_id=row.open_id, open_id=row.open_id)
                if row
                else None
            )

        def lookup_by_chat_id(self, _chat_id):
            return None

    class Credentials:
        def __init__(self):
            self.calls = []

        def ensure_available(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            raise AssertionError("noncohort request reached AI Gateway")

    routing = Routing()
    credentials = Credentials()
    store = billing_identity.BillingIdentityStore(tmp_path / "multitenancy.db")
    preparer = billing_identity.BillingIdentityPreparer(
        routing=routing,
        store=store,
        credentials=credentials,
    )
    homes = {}
    for index in range(6):
        home = tmp_path / f"profile_{index}"
        home.mkdir()
        homes[f"employee_{index}"] = (f"profile_{index}", home)
        homes[f"ou_fixture_{index}"] = (f"profile_{index}", home)

    dispatched = []
    sent = []

    async def enrich(event, _gateway, **_kwargs):
        return event.text

    async def stream(_adapter, _chat_id, _profile, _home, event, **_kwargs):
        dispatched.append(event.raw_event.get("metadata", {}))
        return "ok"

    class Adapter:
        async def send(self, chat_id, text):
            sent.append((chat_id, text))

        async def on_processing_start(self, _event):
            return None

        async def on_processing_complete(self, _event, _outcome):
            return None

        async def edit_message(self, *_args, **_kwargs):
            return None

    monkeypatch.setenv("HERMES_LITELLM_BILLING_ENABLED", "true")
    monkeypatch.setenv(
        "HERMES_LITELLM_BILLING_PAYER_IDS",
        "canary_employee",
    )
    monkeypatch.setattr(billing_identity, "_DEFAULT_PREPARER", preparer)
    monkeypatch.setattr(router_mod, "_get_routing_table", lambda: routing)
    monkeypatch.setattr(
        router_mod,
        "_resolve_or_auto_provision_route",
        lambda sender, alt_id=None: homes.get(sender, (None, None)),
    )
    monkeypatch.setattr(router_mod, "_call_enrich_via_hermes_pipeline", enrich)
    monkeypatch.setattr(router_mod, "_stream_into_feishu", stream)
    monkeypatch.setattr(router_mod, "_is_run_request_seen", lambda _request: False)
    monkeypatch.setattr(router_mod, "_mark_run_request_seen", lambda _request: True)
    gateway = SimpleNamespace(adapters={"feishu": Adapter()})

    for index in range(6):
        for request_index in range(2):
            event = _build_event(
                text=f"fixture-{request_index}",
                chat_id=f"oc_fixture_{index}",
                user_id=f"employee_{index}",
            )
            event.message_id = f"om_fixture_{index}_{request_index}"
            asyncio.run(router_mod.handle_async(event=event, gateway=gateway))

    assert len(dispatched) == 12
    assert credentials.calls == []
    assert all("litellm_billing_enforced" not in metadata for metadata in dispatched)
    assert sent == []


@pytest.mark.asyncio
async def test_feishu_session_store_mark_failure_is_visible_and_retryable(
    monkeypatch,
    tmp_path,
):
    from hermes_multitenancy import billing_identity
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes

    class FlakyStore:
        def __init__(self):
            self.mark_calls = 0
            self.seen = set()

        def is_event_processed(self, event_key, _ttl):
            return event_key in self.seen

        def mark_event_processed(self, event_key, **_kwargs):
            self.mark_calls += 1
            if self.mark_calls == 1:
                raise RuntimeError("session store write failed")
            self.seen.add(event_key)
            return True

    clear_spike_routes()
    add_spike_route("ou_store_retry", tmp_path)
    store = FlakyStore()
    prepare_calls = 0
    sent = []
    dispatches = []

    async def prepare(request, **_kwargs):
        nonlocal prepare_calls
        prepare_calls += 1
        return request

    async def enrich(_event, _gateway, **_kwargs):
        return "enriched"

    async def stream(_adapter, _chat_id, _profile, _home, event, **_kwargs):
        dispatches.append(event.text)
        return "ok"

    class FullFeishuAdapter:
        async def send(self, chat_id, text):
            sent.append((chat_id, text))

        async def on_processing_start(self, _event):
            return None

        async def on_processing_complete(self, _event, _outcome):
            return None

        async def edit_message(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(billing_identity, "prepare_billing_request", prepare)
    monkeypatch.setattr(router_mod, "_call_enrich_via_hermes_pipeline", enrich)
    monkeypatch.setattr(router_mod, "_stream_into_feishu", stream)
    router_mod.override_session_store(store)
    gateway = SimpleNamespace(adapters={"feishu": FullFeishuAdapter()})

    def event():
        value = _build_event(text="hello", user_id="ou_store_retry")
        value.message_id = "om_store_retry"
        return value

    try:
        await router_mod.handle_async(event=event(), gateway=gateway)
        await router_mod.handle_async(event=event(), gateway=gateway)
        await router_mod.handle_async(event=event(), gateway=gateway)
    finally:
        router_mod.override_session_store(None)
        clear_spike_routes()

    assert sent == [("chat-123", "请求状态暂时无法保存，请稍后重试。")]
    assert prepare_calls == 2
    assert store.mark_calls == 2
    assert dispatches == ["enriched"]


@pytest.mark.asyncio
async def test_feishu_duplicate_coalesces_prepare_enrichment_and_admission(
    monkeypatch,
    tmp_path,
):
    from hermes_multitenancy import billing_identity
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes

    clear_spike_routes()
    add_spike_route("ou_feishu_coalesce", tmp_path)
    seen = set()
    prepare_calls = 0
    enrich_calls = 0
    dispatches = []
    enrich_started = asyncio.Event()
    release_enrich = asyncio.Event()

    async def prepare(request, **_kwargs):
        nonlocal prepare_calls
        prepare_calls += 1
        return replace(request, metadata={"litellm_billing_user_id": "employee-1"})

    async def enrich(_event, _gateway, **_kwargs):
        nonlocal enrich_calls
        enrich_calls += 1
        enrich_started.set()
        await release_enrich.wait()
        return "enriched Feishu content"

    def is_seen(request):
        return request.effective_idempotency_key in seen

    def mark_seen(request):
        key = request.effective_idempotency_key
        if key in seen:
            return False
        seen.add(key)
        return True

    async def stream(_adapter, _chat_id, _profile, _home, event, **_kwargs):
        dispatches.append((event.text, event.raw_event["metadata"]))
        return "ok"

    class FullFeishuAdapter:
        async def on_processing_start(self, _event):
            return None

        async def on_processing_complete(self, _event, _outcome):
            return None

        async def edit_message(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(billing_identity, "prepare_billing_request", prepare)
    monkeypatch.setattr(router_mod, "_call_enrich_via_hermes_pipeline", enrich)
    monkeypatch.setattr(router_mod, "_is_run_request_seen", is_seen)
    monkeypatch.setattr(router_mod, "_mark_run_request_seen", mark_seen)
    monkeypatch.setattr(router_mod, "_stream_into_feishu", stream)
    gateway = SimpleNamespace(adapters={"feishu": FullFeishuAdapter()})
    first_event = _build_event(text="same delivery", user_id="ou_feishu_coalesce")
    second_event = _build_event(text="same delivery", user_id="ou_feishu_coalesce")
    first_event.message_id = second_event.message_id = "om_feishu_coalesce"

    try:
        first = asyncio.create_task(router_mod.handle_async(event=first_event, gateway=gateway))
        await enrich_started.wait()
        second = asyncio.create_task(router_mod.handle_async(event=second_event, gateway=gateway))
        await asyncio.sleep(0)

        assert prepare_calls == 1
        assert enrich_calls == 1

        release_enrich.set()
        await asyncio.gather(first, second)

        assert prepare_calls == 1
        assert enrich_calls == 1
        assert len(seen) == 1
        assert dispatches == [
            ("enriched Feishu content", {"litellm_billing_user_id": "employee-1"})
        ]
    finally:
        clear_spike_routes()


@pytest.mark.asyncio
async def test_feishu_without_message_id_keeps_canonical_key_after_enrichment(
    monkeypatch,
    tmp_path,
):
    from hermes_multitenancy import billing_identity
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes

    clear_spike_routes()
    add_spike_route("ou_feishu_content_key", tmp_path)
    seen = set()
    marked = []
    prepare_calls = 0
    enrich_calls = 0
    dispatches = []

    def canonical_key(request):
        record = router_mod._run_request_dedupe_record(request)
        assert record is not None
        return record[0]

    async def prepare(request, **_kwargs):
        nonlocal prepare_calls
        prepare_calls += 1
        return replace(request, metadata={"litellm_billing_user_id": "employee-2"})

    async def enrich(_event, _gateway, **_kwargs):
        nonlocal enrich_calls
        enrich_calls += 1
        return "a different enriched payload that must only be used for dispatch"

    def mark_seen(request):
        key = canonical_key(request)
        marked.append(key)
        if key in seen:
            return False
        seen.add(key)
        return True

    async def stream(_adapter, _chat_id, _profile, _home, event, **_kwargs):
        dispatches.append(event.text)
        return "ok"

    class FullFeishuAdapter:
        async def on_processing_start(self, _event):
            return None

        async def on_processing_complete(self, _event, _outcome):
            return None

        async def edit_message(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(billing_identity, "prepare_billing_request", prepare)
    monkeypatch.setattr(router_mod, "_call_enrich_via_hermes_pipeline", enrich)
    monkeypatch.setattr(
        router_mod,
        "_is_run_request_seen",
        lambda request: canonical_key(request) in seen,
    )
    monkeypatch.setattr(router_mod, "_mark_run_request_seen", mark_seen)
    monkeypatch.setattr(router_mod, "_stream_into_feishu", stream)
    gateway = SimpleNamespace(adapters={"feishu": FullFeishuAdapter()})
    original = "the original Feishu content is intentionally long enough for content dedupe"

    try:
        await router_mod.handle_async(
            event=_build_event(text=original, user_id="ou_feishu_content_key"),
            gateway=gateway,
        )
        await router_mod.handle_async(
            event=_build_event(text=original, user_id="ou_feishu_content_key"),
            gateway=gateway,
        )

        assert prepare_calls == 1
        assert enrich_calls == 1
        assert dispatches == [
            "a different enriched payload that must only be used for dispatch"
        ]
        assert len(marked) == 1
        assert marked[0].startswith("content:")
        assert not marked[0].startswith("idem:")
    finally:
        clear_spike_routes()


@pytest.mark.asyncio
async def test_hook_defers_gateway_processing_complete_for_routed_message(monkeypatch):
    """Base gateway completion must not remove Feishu Typing while router task runs."""
    from hermes_multitenancy import on_pre_gateway_dispatch
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes
    from pathlib import Path
    import tempfile

    clear_spike_routes()
    with tempfile.TemporaryDirectory() as tmp:
        add_spike_route("ou_defer", Path(tmp))
        event = _build_event(user_id="ou_defer")
        event.message_id = "om_defer"
        calls = []

        class MockAdapter:
            def defer_processing_complete(self, ev):
                calls.append(("defer", ev.message_id))

        async def fake_handle_async(*, event, gateway):
            calls.append(("handle", getattr(event, "message_id", None)))

        monkeypatch.setattr("hermes_multitenancy.router.handle_async", fake_handle_async)

        gateway = SimpleNamespace(adapters={"feishu": MockAdapter()})
        result = on_pre_gateway_dispatch(event=event, gateway=gateway, session_store=None)
        await asyncio.sleep(0)

        assert result["action"] == "skip"
        assert calls == [("defer", "om_defer"), ("handle", "om_defer")]

    clear_spike_routes()


@pytest.mark.asyncio
async def test_handle_async_uses_real_open_id_for_explicit_profile_route(monkeypatch, tmp_path):
    """A known Feishu user must route by real ou_* open_id, not SDK short user_id."""
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.runtime import clear_spike_routes

    clear_spike_routes()
    db_path = tmp_path / "routing.db"
    router_mod.override_routing_table(db_path)
    table = router_mod._get_routing_table()
    table.upsert(
        user_id="owner",
        profile_name="coder",
        open_id="ou_owner",
        union_id="on_owner",
    )

    monkeypatch.setattr(
        router_mod,
        "_profile_name_to_home",
        lambda profile_name: tmp_path / "profiles" / profile_name,
    )

    event = _build_event(user_id="g41a5b5g", sender_open_id="ou_owner")
    event.source.user_id_alt = "on_owner"

    dispatched = {}

    class MockPool:
        async def dispatch(self, profile_name, profile_home, agent_event):
            dispatched["profile_name"] = profile_name
            dispatched["profile_home"] = profile_home
            return "ok"

    monkeypatch.setattr(router_mod, "_get_pool", lambda: MockPool())

    await router_mod.handle_async(event=event, gateway=SimpleNamespace(adapters={}))

    assert dispatched == {
        "profile_name": "coder",
        "profile_home": tmp_path / "profiles" / "coder",
    }
    assert table.lookup_by_open_id("g41a5b5g") is None

    router_mod.override_routing_table(None)


def test_handle_async_skips_duplicate_feishu_message_id(monkeypatch, tmp_path):
    """A redelivered Feishu event must not start a second agent run."""
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes

    clear_spike_routes()
    profile_home = tmp_path / "owner"
    profile_home.mkdir()
    add_spike_route("ou_duplicate", profile_home)

    calls = []

    class MockPool:
        async def dispatch(self, profile_name, profile_home, agent_event):
            calls.append((profile_name, agent_event.text))
            return "ok"

    monkeypatch.setattr(router_mod, "_get_pool", lambda: MockPool())

    gateway = SimpleNamespace(adapters={})
    event_a = _build_event(text="check yesterday IT&Sec messages", user_id="ou_duplicate")
    event_a.message_id = "om_same"
    event_b = _build_event(text="check yesterday IT&Sec messages", user_id="ou_duplicate")
    event_b.message_id = "om_same"

    asyncio.run(router_mod.handle_async(event=event_a, gateway=gateway))
    asyncio.run(router_mod.handle_async(event=event_b, gateway=gateway))

    assert calls == [("owner", "check yesterday IT&Sec messages")]
    clear_spike_routes()


def test_handle_async_submits_routed_feishu_run_request_to_broker(monkeypatch, tmp_path):
    """Feishu route should enter the channel-neutral broker before dispatch."""
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes
    from hermes_multitenancy.run_models import RunResult

    clear_spike_routes()
    profile_home = tmp_path / "owner"
    profile_home.mkdir()
    add_spike_route("ou_broker", profile_home)

    admitted = []
    dispatched = []

    class FakePrepared:
        def __init__(self, request):
            self.request = request
            self.internal_metadata = {}

        def with_request(self, request, *, internal_metadata=None):
            prepared = FakePrepared(request)
            prepared.internal_metadata = dict(internal_metadata or {})
            return prepared

    class FakeBroker:
        async def prepare_and_execute(
            self,
            request,
            *,
                execute,
                transform_request,
                before_admit,
                execution_owned,
                entry_completion_owned,
                entry_completion_started,
                on_entry_done,
            ):
                prepared = await transform_request(FakePrepared(request))
                await before_admit(prepared)
                admitted.append(prepared.request)
                execution_owned.set()
                if on_entry_done is not None:
                    entry_completion_owned.set()
                    entry_completion_started.set()
                result = await execute(prepared)
                if on_entry_done is not None:
                    await on_entry_done(False)
                return result

        async def _run_admitted(self, prepared, *, dispatch_agent):
            return RunResult(content="ok", duplicate=False)

    class MockPool:
        async def dispatch(self, profile_name, profile_home, agent_event):
            dispatched.append((profile_name, agent_event.text))
            return "ok"

    monkeypatch.setattr(router_mod, "_make_routed_run_broker", lambda **_kwargs: FakeBroker())
    monkeypatch.setattr(router_mod, "_get_pool", lambda: MockPool())

    event = _build_event(text="hello broker", user_id="ou_broker")
    event.message_id = "om_broker"

    asyncio.run(router_mod.handle_async(event=event, gateway=SimpleNamespace(adapters={})))

    assert len(admitted) == 1
    request = admitted[0]
    assert request.channel == "feishu"
    assert request.profile_name == "owner"
    assert request.user_key == "ou_broker"
    assert request.content == "hello broker"
    assert request.chat_id == "chat-123"
    assert request.message_id == "om_broker"
    assert request.credential_subject == "ou_broker"
    clear_spike_routes()


def test_handle_async_nonstream_dispatch_runs_inside_broker(monkeypatch, tmp_path):
    """Minimal Feishu adapter dispatch should be owned by RunBroker.run."""
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes
    from hermes_multitenancy.run_models import RunResult

    clear_spike_routes()
    profile_home = tmp_path / "owner"
    profile_home.mkdir()
    add_spike_route("ou_nonstream_broker", profile_home)

    broker_calls = []
    pool_calls = []

    class FakePrepared:
        def __init__(self, request):
            self.request = request
            self.internal_metadata = {}

        def with_request(self, request, *, internal_metadata=None):
            prepared = FakePrepared(request)
            prepared.internal_metadata = dict(internal_metadata or {})
            return prepared

    class FakeBroker:
        async def prepare_and_execute(
            self,
            request,
            *,
                execute,
                transform_request,
                before_admit,
                execution_owned,
                entry_completion_owned,
                entry_completion_started,
                on_entry_done,
            ):
                prepared = await transform_request(FakePrepared(request))
                await before_admit(prepared)
                broker_calls.append(("admit", prepared.request.content))
                execution_owned.set()
                if on_entry_done is not None:
                    entry_completion_owned.set()
                    entry_completion_started.set()
                result = await execute(prepared)
                if on_entry_done is not None:
                    await on_entry_done(False)
                return result

        async def _run_admitted(self, prepared, *, dispatch_agent):
            request = prepared.request
            broker_calls.append(("run_admitted", request.content))
            response = await dispatch_agent(request)
            return RunResult(content=response, duplicate=False)

    class MockPool:
        async def dispatch(self, profile_name, profile_home, agent_event):
            pool_calls.append((profile_name, agent_event.text))
            return "ok"

    monkeypatch.setattr(router_mod, "_make_routed_run_broker", lambda **_kwargs: FakeBroker())
    monkeypatch.setattr(router_mod, "_get_pool", lambda: MockPool())

    asyncio.run(router_mod.handle_async(
        event=_build_event(text="hello nonstream broker", user_id="ou_nonstream_broker"),
        gateway=SimpleNamespace(adapters={}),
    ))

    assert broker_calls == [
        ("admit", "hello nonstream broker"),
        ("run_admitted", "hello nonstream broker"),
    ]
    assert pool_calls == [("owner", "hello nonstream broker")]
    clear_spike_routes()


def test_handle_async_streaming_dispatch_runs_inside_broker(monkeypatch, tmp_path):
    """Full Feishu streaming dispatch should be owned by RunBroker.run."""
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes
    from hermes_multitenancy.run_models import RunResult

    clear_spike_routes()
    profile_home = tmp_path / "owner"
    profile_home.mkdir()
    add_spike_route("ou_stream_broker", profile_home)

    broker_calls = []
    stream_calls = []
    media_calls = []
    lifecycle = []

    class FakePrepared:
        def __init__(self, request):
            self.request = request
            self.internal_metadata = {}

        def with_request(self, request, *, internal_metadata=None):
            prepared = FakePrepared(request)
            prepared.internal_metadata = dict(internal_metadata or {})
            return prepared

    class FakeBroker:
        async def prepare_and_execute(
            self,
            request,
            *,
                execute,
                transform_request,
                before_admit,
                execution_owned,
                entry_completion_owned,
                entry_completion_started,
                on_entry_done,
            ):
                prepared = await transform_request(FakePrepared(request))
                await before_admit(prepared)
                broker_calls.append(("admit", prepared.request.content))
                execution_owned.set()
                if on_entry_done is not None:
                    entry_completion_owned.set()
                    entry_completion_started.set()
                result = await execute(prepared)
                if on_entry_done is not None:
                    await on_entry_done(False)
                return result

        async def _run_admitted(self, prepared, *, dispatch_agent):
            request = prepared.request
            broker_calls.append(("run_admitted", request.content))
            response = await dispatch_agent(request)
            return RunResult(content=response, duplicate=False)

    class FullAdapter:
        async def edit_message(self, *args, **kwargs):
            return None

        async def on_processing_start(self, event):
            lifecycle.append(("start", getattr(event, "message_id", None)))

        async def on_processing_complete(self, event, outcome):
            lifecycle.append(("complete", getattr(event, "message_id", None), str(outcome)))

    async def fake_stream(adapter, chat_id, profile_name, profile_home, agent_event, *, messages):
        stream_calls.append((chat_id, profile_name, profile_home.name, agent_event.text, len(messages)))
        return "stream ok"

    async def fake_media(gateway, response_text, agent_event, adapter, profile_home):
        media_calls.append((response_text, agent_event.text, profile_home.name))

    monkeypatch.setattr(
        router_mod,
        "_make_routed_run_broker",
        lambda **_kwargs: FakeBroker(),
    )
    monkeypatch.setattr(router_mod, "_stream_into_feishu", fake_stream)
    monkeypatch.setattr(router_mod, "_deliver_media_from_stream_response", fake_media)

    event = _build_event(text="hello streaming broker", user_id="ou_stream_broker")
    event.message_id = "om_stream_broker"
    asyncio.run(router_mod.handle_async(
        event=event,
        gateway=SimpleNamespace(adapters={"feishu": FullAdapter()}),
    ))

    assert broker_calls == [
        ("admit", "hello streaming broker"),
        ("run_admitted", "hello streaming broker"),
    ]
    assert stream_calls == [("chat-123", "owner", "owner", "hello streaming broker", 1)]
    assert media_calls == [("stream ok", "hello streaming broker", "owner")]
    assert lifecycle == [
        ("start", "om_stream_broker"),
        ("complete", "om_stream_broker", "ProcessingOutcome.SUCCESS"),
    ]
    clear_spike_routes()


def test_handle_async_skips_duplicate_long_content_without_message_id(monkeypatch, tmp_path):
    """Fallback dedupe catches Feishu retries that arrive without a stable message_id."""
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes

    clear_spike_routes()
    profile_home = tmp_path / "owner"
    profile_home.mkdir()
    add_spike_route("ou_duplicate_long", profile_home)

    calls = []

    class MockPool:
        async def dispatch(self, profile_name, profile_home, agent_event):
            calls.append((profile_name, agent_event.text))
            return "ok"

    monkeypatch.setattr(router_mod, "_get_pool", lambda: MockPool())

    text = (
        "检索下昨天it&sec群聊，owner的所有群聊会话，专项诊断下智慧芽、公安相关字样的事件全貌。"
        "更新这篇复盘文档，并给出后续可靠方案。"
    )
    gateway = SimpleNamespace(adapters={})

    asyncio.run(router_mod.handle_async(
        event=_build_event(text=text, user_id="ou_duplicate_long"),
        gateway=gateway,
    ))
    asyncio.run(router_mod.handle_async(
        event=_build_event(text=text, user_id="ou_duplicate_long"),
        gateway=gateway,
    ))

    assert calls == [("owner", text)]
    clear_spike_routes()


def test_handle_async_completes_deferred_processing_for_duplicate(monkeypatch, tmp_path):
    """A duplicate full-Feishu event should close the adapter lifecycle promptly."""
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes

    clear_spike_routes()
    profile_home = tmp_path / "owner"
    profile_home.mkdir()
    add_spike_route("ou_duplicate_full", profile_home)

    class MockPool:
        async def dispatch(self, profile_name, profile_home, agent_event):
            return "ok"

    monkeypatch.setattr(router_mod, "_get_pool", lambda: MockPool())

    event_a = _build_event(text="check yesterday IT&Sec messages", user_id="ou_duplicate_full")
    event_a.message_id = "om_full_same"
    asyncio.run(router_mod.handle_async(event=event_a, gateway=SimpleNamespace(adapters={})))

    calls = []

    class FullAdapter:
        async def edit_message(self, *args, **kwargs):
            raise AssertionError("duplicate should not stream")

        async def on_processing_start(self, event):
            calls.append(("start", event.message_id))

        async def on_processing_complete(self, event, outcome):
            calls.append(("complete", event.message_id, str(outcome)))

        async def complete_deferred_processing(self, event, outcome):
            calls.append(("complete_deferred", event.message_id, str(outcome)))

    event_b = _build_event(text="check yesterday IT&Sec messages", user_id="ou_duplicate_full")
    event_b.message_id = "om_full_same"
    asyncio.run(router_mod.handle_async(
        event=event_b,
        gateway=SimpleNamespace(adapters={"feishu": FullAdapter()}),
    ))

    assert calls == [("complete_deferred", "om_full_same", "ProcessingOutcome.SUCCESS")]
    clear_spike_routes()


@pytest.mark.asyncio
async def test_handle_async_auto_provisions_new_user_to_distinct_profile(monkeypatch, tmp_path):
    """An unseen Feishu sender must get a dedicated profile and continue dispatching."""
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.routing import RoutingTable
    from hermes_multitenancy.runtime import clear_spike_routes

    clear_spike_routes()
    db_path = tmp_path / "routing.db"
    (tmp_path / "config.yaml").write_text(
        "model:\n"
        "  default: glm-5.1\n"
        "  provider: zai\n"
        "platform_toolsets:\n"
        "  feishu:\n"
        "    - feishu_docx\n"
        "platforms:\n"
        "  feishu:\n"
        "    enabled: true\n"
        "    extra:\n"
        "      app_id: test-app\n"
        "      app_secret: test-secret\n",
        encoding="utf-8",
    )
    (tmp_path / "auth.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".env").write_text("ZAI_API_KEY=test-key\n", encoding="utf-8")
    router_mod.override_routing_table(db_path)
    monkeypatch.setattr(
        router_mod,
        "_profile_name_to_home",
        lambda profile_name: tmp_path / "profiles" / profile_name,
    )

    event = _build_event(user_id="ou_new_user")
    event.source.user_id_alt = "on_new_user"

    sent = []
    typing = []
    dispatched = {}

    class MockAdapter:
        async def send_typing(self, chat_id):
            typing.append(chat_id)

        async def send(self, chat_id, content, *, reply_to=None, metadata=None):
            sent.append((chat_id, content))

    class MockPool:
        async def dispatch(self, profile_name, profile_home, agent_event):
            dispatched["profile_name"] = profile_name
            dispatched["profile_home"] = profile_home
            dispatched["text"] = agent_event.text
            return f"[{profile_name}] ok"

    monkeypatch.setattr(router_mod, "_get_pool", lambda: MockPool())

    gateway = SimpleNamespace(adapters={"feishu": MockAdapter()})

    await router_mod.handle_async(event=event, gateway=gateway)

    row = RoutingTable(db_path).lookup_by_open_id("ou_new_user")
    profile_home = tmp_path / "profiles" / "feishu_ou_new_user"

    assert row is not None
    assert row.profile_name == "feishu_ou_new_user"
    assert row.union_id == "on_new_user"
    assert row.profile_name != "coder"
    assert profile_home.is_dir()
    profile_config = (profile_home / "config.yaml")
    assert profile_config.is_file()
    assert "default: zai/glm-5.1" in profile_config.read_text(encoding="utf-8")
    assert "lark-cli" in profile_config.read_text(encoding="utf-8")
    assert "toolsets_mode: explicit" in profile_config.read_text(encoding="utf-8")
    assert "feishu_docx" not in profile_config.read_text(encoding="utf-8")
    assert "app_id: test-app" in profile_config.read_text(encoding="utf-8")
    assert (profile_home / "auth.json").exists()
    assert (profile_home / ".env").exists()
    assert (profile_home / "SOUL.md").is_file()
    assert dispatched == {
        "profile_name": "feishu_ou_new_user",
        "profile_home": profile_home,
        "text": "hi",
    }
    assert typing == ["chat-123"]
    assert sent == [("chat-123", "[feishu_ou_new_user] ok")]

    router_mod.override_routing_table(None)


@pytest.mark.asyncio
async def test_handle_async_does_not_auto_provision_unknown_user_when_disabled(
    monkeypatch, tmp_path
):
    """Disabling user auto-provision still blocks unknown personal fallback profiles."""
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.routing import RoutingTable
    from hermes_multitenancy.runtime import clear_spike_routes

    clear_spike_routes()
    db_path = tmp_path / "routing.db"
    router_mod.override_routing_table(db_path)
    monkeypatch.setenv("HERMES_MULTITENANCY_AUTO_PROVISION", "0")
    monkeypatch.setattr(
        router_mod,
        "_profile_name_to_home",
        lambda profile_name: tmp_path / "profiles" / profile_name,
    )

    dispatched = []

    class MockPool:
        async def dispatch(self, profile_name, profile_home, agent_event):
            dispatched.append((profile_name, profile_home))
            return "ok"

    monkeypatch.setattr(router_mod, "_get_pool", lambda: MockPool())

    await router_mod.handle_async(
        event=_build_event(user_id="ou_unknown_personal"),
        gateway=SimpleNamespace(adapters={}),
    )

    table = RoutingTable(db_path)
    assert table.lookup_by_open_id("ou_unknown_personal") is None
    assert not (tmp_path / "profiles" / "feishu_ou_unknown_personal").exists()
    assert dispatched == []

    table.close()
    router_mod.override_routing_table(None)


def test_auto_profile_config_does_not_invent_default_model():
    from hermes_multitenancy.router import _normalize_profile_config

    assert "model" not in _normalize_profile_config({})
    normalized = _normalize_profile_config({"tools": ["web"]})
    assert "model" not in normalized
    assert normalized["tools"] == ["web"]


@pytest.mark.asyncio
async def test_new_open_id_auto_provisions_before_stale_alt_route(monkeypatch, tmp_path):
    """A new app-scoped Feishu open_id must not be absorbed by an old union route."""
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.routing import RoutingTable
    from hermes_multitenancy.runtime import clear_spike_routes

    clear_spike_routes()
    db_path = tmp_path / "routing.db"
    (tmp_path / "config.yaml").write_text(
        "model:\n"
        "  default: zai/glm-5.1\n"
        "platform_toolsets:\n"
        "  feishu:\n"
        "    - feishu_user_info\n",
        encoding="utf-8",
    )
    router_mod.override_routing_table(db_path)
    table = router_mod._get_routing_table()
    table.upsert(
        user_id="alice",
        profile_name="spike_alice",
        open_id="on_existing_user",
        union_id="on_existing_user",
    )
    monkeypatch.setattr(
        router_mod,
        "_profile_name_to_home",
        lambda profile_name: tmp_path / "profiles" / profile_name,
    )

    event = _build_event(user_id="short_brand_new", sender_open_id="ou_brand_new")
    event.source.user_id_alt = "on_existing_user"

    dispatched = {}

    class MockPool:
        async def dispatch(self, profile_name, profile_home, agent_event):
            dispatched["profile_name"] = profile_name
            dispatched["profile_home"] = profile_home
            return "ok"

    monkeypatch.setattr(router_mod, "_get_pool", lambda: MockPool())

    await router_mod.handle_async(
        event=event,
        gateway=SimpleNamespace(adapters={}),
    )

    fresh_table = RoutingTable(db_path)
    new_row = fresh_table.lookup_by_open_id("ou_brand_new")
    old_row = fresh_table.lookup_by_open_id("on_existing_user")

    assert new_row is not None
    assert new_row.profile_name == "feishu_ou_brand_new"
    assert new_row.union_id == "on_existing_user"
    assert old_row is not None
    assert old_row.profile_name == "spike_alice"
    assert dispatched["profile_name"] == "feishu_ou_brand_new"
    assert dispatched["profile_home"] == tmp_path / "profiles" / "feishu_ou_brand_new"
    assert fresh_table.lookup_by_open_id("short_brand_new") is None

    fresh_table.close()
    router_mod.override_routing_table(None)


@pytest.mark.asyncio
async def test_legacy_alt_route_used_when_real_open_id_unavailable(monkeypatch, tmp_path):
    """If no ou_* is present, a legacy alt route should win over auto-provision."""
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.runtime import clear_spike_routes

    clear_spike_routes()
    db_path = tmp_path / "routing.db"
    router_mod.override_routing_table(db_path)
    table = router_mod._get_routing_table()
    table.upsert(
        user_id="legacy",
        profile_name="legacy_profile",
        open_id="on_legacy_user",
        union_id="on_legacy_user",
    )
    monkeypatch.setattr(
        router_mod,
        "_profile_name_to_home",
        lambda profile_name: tmp_path / "profiles" / profile_name,
    )

    event = _build_event(user_id="short_without_open_id")
    event.source.user_id_alt = "on_legacy_user"
    dispatched = {}

    class MockPool:
        async def dispatch(self, profile_name, profile_home, agent_event):
            dispatched["profile_name"] = profile_name
            dispatched["profile_home"] = profile_home
            return "ok"

    monkeypatch.setattr(router_mod, "_get_pool", lambda: MockPool())

    await router_mod.handle_async(event=event, gateway=SimpleNamespace(adapters={}))

    assert dispatched == {
        "profile_name": "legacy_profile",
        "profile_home": tmp_path / "profiles" / "legacy_profile",
    }
    assert table.lookup_by_open_id("short_without_open_id") is None

    router_mod.override_routing_table(None)


@pytest.mark.asyncio
async def test_handle_async_sets_resolved_raw_event_open_id_on_agent_event(monkeypatch, tmp_path):
    """The sender selected by router must be visible to AIAgent/subprocess payload code."""
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes

    clear_spike_routes()
    profile_home = tmp_path / "raw-sender"
    profile_home.mkdir()
    add_spike_route("ou_raw_event_sender", profile_home)

    event = _build_event(user_id="short_sender_without_ou")
    event.raw_event = {
        "event": {
            "message": {
                "sender": {
                    "sender_id": {
                        "open_id": "ou_raw_event_sender",
                        "union_id": "on_raw_event_sender",
                    }
                }
            }
        }
    }
    captured = {}

    class MockPool:
        async def dispatch(self, profile_name, profile_home, agent_event):
            captured["profile_name"] = profile_name
            captured["sender_open_id"] = getattr(agent_event, "sender_open_id", None)
            return "ok"

    monkeypatch.setattr(router_mod, "_get_pool", lambda: MockPool())

    await router_mod.handle_async(event=event, gateway=SimpleNamespace(adapters={}))

    assert captured == {
        "profile_name": "raw-sender",
        "sender_open_id": "ou_raw_event_sender",
    }

    clear_spike_routes()


@pytest.mark.asyncio
async def test_auto_provision_normalizes_existing_profile_config(monkeypatch, tmp_path):
    """Existing auto profiles are repaired if their model.default lacks provider."""
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.runtime import clear_spike_routes

    clear_spike_routes()
    db_path = tmp_path / "routing.db"
    (tmp_path / "config.yaml").write_text(
        "platforms:\n"
        "  feishu:\n"
        "    enabled: true\n"
        "    extra:\n"
        "      app_id: repair-app\n"
        "      app_secret: repair-secret\n",
        encoding="utf-8",
    )
    profile_home = tmp_path / "profiles" / "feishu_ou_existing"
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text(
        "model:\n"
        "  default: glm-5.1\n"
        "  provider: zai\n",
        encoding="utf-8",
    )
    router_mod.override_routing_table(db_path)
    monkeypatch.setattr(
        router_mod,
        "_profile_name_to_home",
        lambda profile_name: tmp_path / "profiles" / profile_name,
    )

    class MockPool:
        async def dispatch(self, profile_name, profile_home, agent_event):
            return "ok"

    monkeypatch.setattr(router_mod, "_get_pool", lambda: MockPool())

    event = _build_event(user_id="ou_existing")
    await router_mod.handle_async(
        event=event,
        gateway=SimpleNamespace(adapters={}),
    )

    assert "default: zai/glm-5.1" in (profile_home / "config.yaml").read_text(encoding="utf-8")
    assert "app_id: repair-app" in (profile_home / "config.yaml").read_text(encoding="utf-8")

    router_mod.override_routing_table(None)


@pytest.mark.asyncio
async def test_existing_auto_profile_route_repairs_config_on_dispatch(monkeypatch, tmp_path):
    """Already-routed auto profiles are repaired before AIAgent dispatch."""
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.runtime import clear_spike_routes

    clear_spike_routes()
    db_path = tmp_path / "routing.db"
    (tmp_path / "config.yaml").write_text(
        "platforms:\n"
        "  feishu:\n"
        "    enabled: true\n"
        "    extra:\n"
        "      app_id: routed-repair-app\n"
        "      app_secret: routed-repair-secret\n",
        encoding="utf-8",
    )
    profile_home = tmp_path / "profiles" / "feishu_ou_existing"
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text(
        "model:\n"
        "  default: glm-5.1\n"
        "  provider: zai\n",
        encoding="utf-8",
    )
    router_mod.override_routing_table(db_path)
    table = router_mod._get_routing_table()
    table.upsert(
        user_id="ou_existing",
        profile_name="feishu_ou_existing",
        open_id="ou_existing",
        union_id=None,
    )
    monkeypatch.setattr(
        router_mod,
        "_profile_name_to_home",
        lambda profile_name: tmp_path / "profiles" / profile_name,
    )

    class MockPool:
        async def dispatch(self, profile_name, profile_home, agent_event):
            return "ok"

    monkeypatch.setattr(router_mod, "_get_pool", lambda: MockPool())

    await router_mod.handle_async(
        event=_build_event(user_id="ou_existing"),
        gateway=SimpleNamespace(adapters={}),
    )

    assert "default: zai/glm-5.1" in (profile_home / "config.yaml").read_text(encoding="utf-8")
    assert "app_id: routed-repair-app" in (profile_home / "config.yaml").read_text(encoding="utf-8")

    router_mod.override_routing_table(None)


@pytest.mark.asyncio
async def test_handle_async_streams_enriched_text_to_aiagent(monkeypatch, tmp_path):
    """File-only Feishu messages have empty event.text; stream path must use enrichment."""
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes

    clear_spike_routes()
    add_spike_route("ou_file_sender", tmp_path)

    event = _build_event(text="", user_id="ou_file_sender")
    event.message_id = "om_file"
    event.media_urls = ["/tmp/hermes-file.md"]
    event.media_types = ["text/plain"]

    captured = {}

    async def fake_enrich(event, gateway):
        return "[Content of hermes-file.md]:\nhello from uploaded file"

    async def fake_stream(adapter, chat_id, profile_name, profile_home, event, *, messages=None):
        captured["event_text"] = event.text
        captured["user_message"] = messages[-1]["content"]
        return "read it"

    class FullFeishuAdapter:
        async def on_processing_start(self, event):
            return None

        async def on_processing_complete(self, event, outcome):
            return None

        async def edit_message(self, *args, **kwargs):
            return None

    monkeypatch.setattr(router_mod, "_enrich_via_hermes_pipeline", fake_enrich)
    monkeypatch.setattr(router_mod, "_stream_into_feishu", fake_stream)

    gateway = SimpleNamespace(adapters={"feishu": FullFeishuAdapter()})

    await router_mod.handle_async(event=event, gateway=gateway)

    assert captured["event_text"] == "[Content of hermes-file.md]:\nhello from uploaded file"
    assert captured["user_message"] == "[Content of hermes-file.md]:\nhello from uploaded file"

    clear_spike_routes()


@pytest.mark.asyncio
async def test_handle_async_short_circuits_when_image_vision_is_unavailable(monkeypatch, tmp_path):
    """A broken vision provider should produce a clear blocked reply, not a tool-call timeout."""
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes

    clear_spike_routes()
    profile_home = tmp_path / "vision-profile"
    profile_home.mkdir()
    add_spike_route("ou_image_sender", profile_home)

    event = _build_event(text="", user_id="ou_image_sender")
    event.message_id = "om_image"
    event.media_urls = [str(profile_home / "cache" / "images" / "inbound_jpg_FEISHU_MEDIA_FILE_JPG_TEST.jpg")]
    event.media_types = ["image/jpeg"]

    async def fake_enrich(event, gateway):
        return (
            "[The user sent an image but something went wrong when I tried to look at it~ "
            "You can try examining it yourself with vision_analyze using image_url: "
            f"{event.media_urls[0]}]"
        )

    async def fake_stream(*args, **kwargs):
        raise AssertionError("image vision unavailable should not dispatch to AIAgent")

    sent = []

    class FullFeishuAdapter:
        async def send(self, chat_id, text):
            sent.append((chat_id, text))

        async def on_processing_start(self, event):
            return None

        async def on_processing_complete(self, event, outcome):
            return None

        async def edit_message(self, *args, **kwargs):
            return None

    monkeypatch.setattr(router_mod, "_enrich_via_hermes_pipeline", fake_enrich)
    monkeypatch.setattr(router_mod, "_stream_into_feishu", fake_stream)

    await router_mod.handle_async(event=event, gateway=SimpleNamespace(adapters={"feishu": FullFeishuAdapter()}))

    assert sent
    assert sent[0][0] == "chat-123"
    assert "无法读取图片内容" in sent[0][1]
    assert "vision_analyze" in sent[0][1]
    assert "/Users/" not in sent[0][1]
    assert "/cache/images/" not in sent[0][1]
    assert "FEISHU_MEDIA_FILE_JPG_TEST" in sent[0][1]

    clear_spike_routes()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "expected_outcome"),
    [
        ("vision_success", "ProcessingOutcome.SUCCESS"),
        ("vision_send_failure", "ProcessingOutcome.FAILURE"),
        ("billing_failure", "ProcessingOutcome.FAILURE"),
        ("session_store_unavailable", "ProcessingOutcome.FAILURE"),
        ("durable_mark_failure", "ProcessingOutcome.FAILURE"),
        ("duplicate", "ProcessingOutcome.SUCCESS"),
    ],
)
async def test_hook_completes_deferred_processing_once_across_admission_outcomes(
    monkeypatch,
    tmp_path,
    case,
    expected_outcome,
):
    """The hook must hand completion to exactly one pre- or post-admission owner."""
    from hermes_multitenancy import billing_identity, on_pre_gateway_dispatch
    from hermes_multitenancy.run_broker import RunRejected
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes

    clear_spike_routes()
    profile_home = tmp_path / case
    profile_home.mkdir()
    user_id = f"ou_{case}"
    message_id = f"om_{case}"
    add_spike_route(user_id, profile_home)

    event = _build_event(text="hello", user_id=user_id)
    event.message_id = message_id
    if case.startswith("vision_"):
        event.text = ""
        event.media_urls = [str(profile_home / "cache" / "images" / "hook.jpg")]
        event.media_types = ["image/jpeg"]
    calls = []
    completed = asyncio.Event()
    prepare_calls = 0
    enrich_calls = 0
    dispatch_calls = 0

    async def prepare(request, **_kwargs):
        nonlocal prepare_calls
        prepare_calls += 1
        if case == "billing_failure":
            raise RunRejected("LiteLLM management API is unavailable")
        return request

    async def fake_enrich(value, _gateway):
        nonlocal enrich_calls
        enrich_calls += 1
        if case.startswith("vision_"):
            return (
                "[The user sent an image but something went wrong when I tried to look at it~ "
                "You can try examining it yourself with vision_analyze using image_url: "
                f"{value.media_urls[0]}]"
            )
        return "enriched"

    def mark_seen(request):
        if case == "durable_mark_failure":
            raise RuntimeError("session store write failed")
        return True

    async def fake_stream(*_args, **_kwargs):
        nonlocal dispatch_calls
        dispatch_calls += 1
        return "ok"

    class FullFeishuAdapter:
        def defer_processing_complete(self, value):
            calls.append(("defer", value.message_id))

        async def send(self, _chat_id, text):
            calls.append(("send", text))
            if case == "vision_send_failure" and "vision_analyze" in text:
                raise RuntimeError("temporary Feishu send failure")

        async def on_processing_start(self, _event):
            calls.append(("start", _event.message_id))

        async def on_processing_complete(self, *_args):
            raise AssertionError("deferred lifecycle should use its matching completion API")

        async def complete_deferred_processing(self, value, outcome):
            calls.append(("complete_deferred", value.message_id, str(outcome)))
            completed.set()

        async def edit_message(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(billing_identity, "prepare_billing_request", prepare)
    monkeypatch.setattr(router_mod, "_enrich_via_hermes_pipeline", fake_enrich)
    monkeypatch.setattr(router_mod, "_stream_into_feishu", fake_stream)
    if case == "session_store_unavailable":
        monkeypatch.setattr(router_mod, "_get_session_store", lambda: None)
    else:
        monkeypatch.setattr(
            router_mod,
            "_is_run_request_seen",
            lambda _request: case == "duplicate",
        )
        monkeypatch.setattr(router_mod, "_mark_run_request_seen", mark_seen)
    gateway = SimpleNamespace(adapters={"feishu": FullFeishuAdapter()})

    try:
        result = on_pre_gateway_dispatch(
            event=event,
            gateway=gateway,
            session_store=None,
        )
        await asyncio.wait_for(completed.wait(), timeout=2)
        await asyncio.sleep(0.05)

        assert result == {
            "action": "skip",
            "reason": "multitenancy router took over",
        }
        assert calls[0] == ("defer", message_id)
        assert [call for call in calls if call[0] == "complete_deferred"] == [
            ("complete_deferred", message_id, expected_outcome)
        ]
        assert not [call for call in calls if call[0] == "start"]
        if case == "session_store_unavailable":
            assert [call[1] for call in calls if call[0] == "send"] == [
                "请求状态暂时无法保存，请稍后重试。"
            ]
            assert (prepare_calls, enrich_calls, dispatch_calls) == (0, 0, 0)
    finally:
        clear_spike_routes()


@pytest.mark.asyncio
async def test_hook_leader_cancel_with_live_peer_completes_deferred_processing_once(
    monkeypatch,
    tmp_path,
):
    """A cancelled broker waiter must not complete a shared Feishu lifecycle twice."""
    from contextlib import suppress

    from hermes_multitenancy import billing_identity
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy import run_broker as run_broker_mod
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes

    clear_spike_routes()
    profile_home = tmp_path / "leader-cancel-live-peer"
    profile_home.mkdir()
    add_spike_route("ou_leader_cancel_peer", profile_home)

    prepare_started = asyncio.Event()
    release_prepare = asyncio.Event()
    completed = asyncio.Event()
    tracked_tasks = []
    lifecycle = []
    marked = []
    dispatches = 0

    async def prepare(request, **_kwargs):
        prepare_started.set()
        await release_prepare.wait()
        return request

    async def fake_enrich(_event, _gateway):
        return "enriched"

    def mark_seen(request):
        marked.append(request.effective_idempotency_key)
        return True

    async def fake_stream(*_args, **_kwargs):
        nonlocal dispatches
        dispatches += 1
        return "ok"

    class FullFeishuAdapter:
        def defer_processing_complete(self, value):
            lifecycle.append(("defer", value.message_id))

        async def complete_deferred_processing(self, value, outcome):
            lifecycle.append(("complete", value.message_id, str(outcome)))
            completed.set()

        async def on_processing_complete(self, *_args):
            raise AssertionError("deferred lifecycle must use the matching completion API")

        async def on_processing_start(self, value):
            lifecycle.append(("start", value.message_id))

        async def send(self, *_args, **_kwargs):
            return None

        async def edit_message(self, *_args, **_kwargs):
            return None

    original_handle_async = router_mod.handle_async

    async def tracked_handle_async(*, event, gateway):
        tracked_tasks.append(asyncio.current_task())
        return await original_handle_async(event=event, gateway=gateway)

    monkeypatch.setattr(billing_identity, "prepare_billing_request", prepare)
    monkeypatch.setattr(router_mod, "handle_async", tracked_handle_async)
    monkeypatch.setattr(router_mod, "_enrich_via_hermes_pipeline", fake_enrich)
    monkeypatch.setattr(router_mod, "_is_run_request_seen", lambda _request: False)
    monkeypatch.setattr(router_mod, "_mark_run_request_seen", mark_seen)
    monkeypatch.setattr(router_mod, "_stream_into_feishu", fake_stream)
    gateway = SimpleNamespace(adapters={"feishu": FullFeishuAdapter()})

    def event():
        value = _build_event(text="hello", user_id="ou_leader_cancel_peer")
        value.message_id = "om_leader_cancel_peer"
        return value

    async def wait_for_two_waiters():
        for _ in range(200):
            entries = list(run_broker_mod._EXECUTION_INFLIGHT.values())
            if len(tracked_tasks) >= 2 and any(entry.waiters == 2 for entry in entries):
                return
            await asyncio.sleep(0)
        raise AssertionError("concurrent Feishu requests did not join one broker entry")

    try:
        router_mod.on_pre_gateway_dispatch(event=event(), gateway=gateway)
        await asyncio.wait_for(prepare_started.wait(), timeout=2)
        router_mod.on_pre_gateway_dispatch(event=event(), gateway=gateway)
        await asyncio.wait_for(wait_for_two_waiters(), timeout=2)

        leader_task = tracked_tasks[0]
        peer_task = tracked_tasks[1]
        assert leader_task is not None and peer_task is not None
        leader_task.cancel()
        with suppress(asyncio.CancelledError):
            await leader_task

        release_prepare.set()
        await asyncio.wait_for(peer_task, timeout=2)
        await asyncio.wait_for(completed.wait(), timeout=2)
        await asyncio.sleep(0)

        assert [call for call in lifecycle if call[0] == "defer"] == [
            ("defer", "om_leader_cancel_peer"),
            ("defer", "om_leader_cancel_peer"),
        ]
        assert [call for call in lifecycle if call[0] == "complete"] == [
            ("complete", "om_leader_cancel_peer", "ProcessingOutcome.SUCCESS")
        ]
        assert len(marked) == 1
        assert dispatches == 1
    finally:
        release_prepare.set()
        for task in tracked_tasks:
            if task is not None and not task.done():
                task.cancel()
        if tracked_tasks:
            await asyncio.gather(
                *(task for task in tracked_tasks if task is not None),
                return_exceptions=True,
            )
        clear_spike_routes()


@pytest.mark.asyncio
async def test_hook_late_defer_after_shared_completion_gets_new_completion(
    monkeypatch,
    tmp_path,
):
    """A defer after the old finalizer snapshot must not inherit that generation."""
    from hermes_multitenancy import billing_identity
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.run_broker import RunBroker
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes

    clear_spike_routes()
    profile_home = tmp_path / "late-defer-generation"
    profile_home.mkdir()
    add_spike_route("ou_late_defer", profile_home)

    old_finalizer_done = asyncio.Event()
    release_entry_cleanup = asyncio.Event()
    tracked_tasks = []
    completions = []
    marked = []
    dispatches = 0

    async def prepare(request, **_kwargs):
        return request

    async def fake_enrich(_event, _gateway):
        return "enriched"

    def mark_seen(request):
        marked.append(request.effective_idempotency_key)
        return True

    async def fake_stream(*_args, **_kwargs):
        nonlocal dispatches
        dispatches += 1
        return "ok"

    class FullFeishuAdapter:
        def __init__(self):
            self.deferred = set()

        def defer_processing_complete(self, value):
            self.deferred.add(value.message_id)

        async def complete_deferred_processing(self, value, outcome):
            self.deferred.discard(value.message_id)
            completions.append((value.message_id, str(outcome)))

        async def on_processing_complete(self, *_args):
            raise AssertionError("deferred lifecycle must use the matching completion API")

        async def on_processing_start(self, _value):
            return None

        async def send(self, *_args, **_kwargs):
            return None

        async def edit_message(self, *_args, **_kwargs):
            return None

    original_prepare_entry = RunBroker._prepare_admit_execute_once

    async def hold_entry_after_finalizer(self, *args, **kwargs):
        result = await original_prepare_entry(self, *args, **kwargs)
        old_finalizer_done.set()
        await release_entry_cleanup.wait()
        return result

    original_handle_async = router_mod.handle_async

    async def tracked_handle_async(*, event, gateway):
        tracked_tasks.append(asyncio.current_task())
        return await original_handle_async(event=event, gateway=gateway)

    monkeypatch.setattr(billing_identity, "prepare_billing_request", prepare)
    monkeypatch.setattr(RunBroker, "_prepare_admit_execute_once", hold_entry_after_finalizer)
    monkeypatch.setattr(router_mod, "handle_async", tracked_handle_async)
    monkeypatch.setattr(router_mod, "_enrich_via_hermes_pipeline", fake_enrich)
    monkeypatch.setattr(router_mod, "_is_run_request_seen", lambda _request: False)
    monkeypatch.setattr(router_mod, "_mark_run_request_seen", mark_seen)
    monkeypatch.setattr(router_mod, "_stream_into_feishu", fake_stream)
    adapter = FullFeishuAdapter()
    gateway = SimpleNamespace(adapters={"feishu": adapter})

    def event():
        value = _build_event(text="hello", user_id="ou_late_defer")
        value.message_id = "om_late_defer_generation"
        return value

    try:
        router_mod.on_pre_gateway_dispatch(event=event(), gateway=gateway)
        await asyncio.wait_for(old_finalizer_done.wait(), timeout=2)
        assert completions == [
            ("om_late_defer_generation", "ProcessingOutcome.SUCCESS")
        ]
        assert adapter.deferred == set()

        router_mod.on_pre_gateway_dispatch(event=event(), gateway=gateway)
        for _ in range(200):
            if len(tracked_tasks) >= 2 and "om_late_defer_generation" in adapter.deferred:
                break
            await asyncio.sleep(0)
        else:
            raise AssertionError("late hook did not register its defer generation")

        release_entry_cleanup.set()
        await asyncio.wait_for(
            asyncio.gather(*(task for task in tracked_tasks if task is not None)),
            timeout=2,
        )

        assert completions == [
            ("om_late_defer_generation", "ProcessingOutcome.SUCCESS"),
            ("om_late_defer_generation", "ProcessingOutcome.SUCCESS"),
        ]
        assert adapter.deferred == set()
        assert len(marked) == 1
        assert dispatches == 1
    finally:
        release_entry_cleanup.set()
        for task in tracked_tasks:
            if task is not None and not task.done():
                task.cancel()
        if tracked_tasks:
            await asyncio.gather(
                *(task for task in tracked_tasks if task is not None),
                return_exceptions=True,
            )
        clear_spike_routes()


def test_feishu_completion_generation_state_has_hard_bound():
    from hermes_multitenancy.router import feishu_completion

    adapter = SimpleNamespace()
    events = []
    for index in range(feishu_completion._MAX_COMPLETED_STATES):
        event = SimpleNamespace(message_id=f"om_generation_{index}")
        events.append(event)
        feishu_completion.register_deferred_completion(adapter, event)

    overflow = SimpleNamespace(message_id="om_generation_overflow")
    feishu_completion.register_deferred_completion(adapter, overflow)
    states = getattr(adapter, feishu_completion._STATES_ATTR)

    assert len(states) == feishu_completion._MAX_COMPLETED_STATES
    assert not feishu_completion.has_deferred_completion_claim(adapter, overflow)

    feishu_completion.begin_deferred_completion(adapter, events[0])
    replacement = SimpleNamespace(message_id="om_generation_replacement")
    feishu_completion.register_deferred_completion(adapter, replacement)

    assert len(states) == feishu_completion._MAX_COMPLETED_STATES
    assert events[0].message_id not in states
    assert feishu_completion.has_deferred_completion_claim(adapter, replacement)


@pytest.mark.asyncio
async def test_vision_block_send_failure_does_not_mark_and_retry_is_deduped(
    monkeypatch,
    tmp_path,
):
    from hermes_multitenancy import billing_identity
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes

    clear_spike_routes()
    profile_home = tmp_path / "vision-retry-profile"
    profile_home.mkdir()
    add_spike_route("ou_image_retry", profile_home)
    seen = set()
    prepare_calls = 0
    enrich_calls = 0
    send_attempts = 0
    error_replies = []
    marked = []
    send_started = asyncio.Event()
    release_send = asyncio.Event()

    async def prepare(request, **_kwargs):
        nonlocal prepare_calls
        prepare_calls += 1
        return request

    async def fake_enrich(event, _gateway):
        nonlocal enrich_calls
        enrich_calls += 1
        return (
            "[The user sent an image but something went wrong when I tried to look at it~ "
            "You can try examining it yourself with vision_analyze using image_url: "
            f"{event.media_urls[0]}]"
        )

    def is_seen(request):
        return request.effective_idempotency_key in seen

    def mark_seen(request):
        marked.append(request.effective_idempotency_key)
        if request.effective_idempotency_key in seen:
            return False
        seen.add(request.effective_idempotency_key)
        return True

    class FullFeishuAdapter:
        async def send(self, _chat_id, _text):
            nonlocal send_attempts
            if _text == "请求状态暂时无法保存，请稍后重试。":
                error_replies.append(_text)
                return
            send_attempts += 1
            if send_attempts == 1:
                raise RuntimeError("temporary Feishu send failure")
            send_started.set()
            await release_send.wait()

        async def on_processing_start(self, _event):
            return None

        async def on_processing_complete(self, _event, _outcome):
            return None

        async def edit_message(self, *_args, **_kwargs):
            return None

    async def must_not_dispatch(*_args, **_kwargs):
        raise AssertionError("vision-block reply must not enter normal agent dispatch")

    monkeypatch.setattr(billing_identity, "prepare_billing_request", prepare)
    monkeypatch.setattr(router_mod, "_enrich_via_hermes_pipeline", fake_enrich)
    monkeypatch.setattr(router_mod, "_is_run_request_seen", is_seen)
    monkeypatch.setattr(router_mod, "_mark_run_request_seen", mark_seen)
    monkeypatch.setattr(router_mod, "_stream_into_feishu", must_not_dispatch)
    gateway = SimpleNamespace(adapters={"feishu": FullFeishuAdapter()})

    def event():
        value = _build_event(text="", user_id="ou_image_retry")
        value.message_id = "om_image_retry"
        value.media_urls = [str(profile_home / "cache" / "images" / "retry.jpg")]
        value.media_types = ["image/jpeg"]
        return value

    try:
        await router_mod.handle_async(event=event(), gateway=gateway)
        assert marked == []

        retry = asyncio.create_task(router_mod.handle_async(event=event(), gateway=gateway))
        await send_started.wait()
        peer = asyncio.create_task(router_mod.handle_async(event=event(), gateway=gateway))
        await asyncio.sleep(0)
        assert prepare_calls == 2
        assert enrich_calls == 2
        assert send_attempts == 2
        assert error_replies == ["请求状态暂时无法保存，请稍后重试。"]
        release_send.set()
        await asyncio.gather(retry, peer)
        await router_mod.handle_async(event=event(), gateway=gateway)

        assert prepare_calls == 2
        assert enrich_calls == 2
        assert send_attempts == 2
        assert len(marked) == 1
        assert marked == list(seen)
    finally:
        clear_spike_routes()


def test_image_vision_unavailable_response_reports_timeout_without_provider_repair(tmp_path):
    """Image preprocessing timeout should not tell users to fix provider credentials."""
    from hermes_multitenancy import router as router_mod

    event = SimpleNamespace(
        message_id="om_timeout",
        media_urls=[str(tmp_path / "cache" / "images" / "img_timeout.jpg")],
        media_types=["image/jpeg"],
    )
    enriched = (
        "[The user sent an image but vision auto-analysis timed out before vision_analyze completed "
        f"using image_url: {event.media_urls[0]}. Feishu message_id: om_timeout.]"
    )

    response = router_mod._image_vision_unavailable_response(event, enriched)

    assert response is not None
    assert "超时" in response
    assert "provider/key" not in response
    assert "provider rejected" not in response
    assert "om_timeout" in response


@pytest.mark.asyncio
async def test_handle_async_reuses_gateway_media_delivery_after_stream(monkeypatch, tmp_path):
    """Multitenant streaming must reuse Hermes' native MEDIA:<path> delivery path."""
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes

    clear_spike_routes()
    add_spike_route("ou_media_sender", tmp_path)

    event = _build_event(text="make a file", user_id="ou_media_sender")
    event.message_id = "om_media"
    outbound_path = tmp_path / "cache" / "documents" / "hermes-output.md"
    outbound_path.parent.mkdir(parents=True)
    outbound_path.write_text("profile-scoped outbound file", encoding="utf-8")

    async def fake_enrich(event, gateway):
        return "make a file"

    async def fake_stream(adapter, chat_id, profile_name, profile_home, event, *, messages=None):
        assert profile_home == tmp_path
        return f"created\nMEDIA:{outbound_path}"

    class FullFeishuAdapter:
        async def on_processing_start(self, event):
            return None

        async def on_processing_complete(self, event, outcome):
            return None

        async def edit_message(self, *args, **kwargs):
            return None

    delivered = []

    async def deliver_media(response, delivered_event, adapter):
        delivered.append(
            {
                "response": response,
                "event_text": delivered_event.text,
                "chat_id": delivered_event.source.chat_id,
                "adapter": adapter,
            }
        )

    adapter = FullFeishuAdapter()
    gateway = SimpleNamespace(
        adapters={"feishu": adapter},
        _deliver_media_from_response=deliver_media,
    )

    monkeypatch.setattr(router_mod, "_enrich_via_hermes_pipeline", fake_enrich)
    monkeypatch.setattr(router_mod, "_stream_into_feishu", fake_stream)

    await router_mod.handle_async(event=event, gateway=gateway)

    assert delivered == [
        {
            "response": f"created\nMEDIA:{outbound_path}",
            "event_text": "make a file",
            "chat_id": "chat-123",
            "adapter": adapter,
        }
    ]

    clear_spike_routes()


@pytest.mark.asyncio
async def test_handle_async_auto_delivers_plain_profile_file_path(monkeypatch, tmp_path):
    """Plain profile-local file paths in replies become Feishu files and WebUI workspace artifacts."""
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes

    clear_spike_routes()
    profile_home = tmp_path / "profiles" / "plain-path-profile"
    profile_home.mkdir(parents=True)
    add_spike_route("ou_plain_file", profile_home)

    report = profile_home / ".ai-docs" / "kep-prd-analysis" / "技术方案_AI饮食记录_20260515.md"
    report.parent.mkdir(parents=True)
    report.write_text("profile report", encoding="utf-8")

    async def fake_enrich(event, gateway):
        return "make report"

    async def fake_stream(adapter, chat_id, profile_name, profile_home, event, *, messages=None):
        return f"done: {report}"

    class FullFeishuAdapter:
        async def on_processing_start(self, event):
            return None

        async def on_processing_complete(self, event, outcome):
            return None

        async def edit_message(self, *args, **kwargs):
            return None

    delivered = []

    async def deliver_media(response, delivered_event, adapter):
        delivered.append(response)

    monkeypatch.setattr(router_mod, "_enrich_via_hermes_pipeline", fake_enrich)
    monkeypatch.setattr(router_mod, "_stream_into_feishu", fake_stream)

    gateway = SimpleNamespace(
        adapters={"feishu": FullFeishuAdapter()},
        _deliver_media_from_response=deliver_media,
    )

    await router_mod.handle_async(event=_build_event(text="make report", user_id="ou_plain_file"), gateway=gateway)

    workspace_report = profile_home / "workspace" / "Downloads" / report.name
    assert workspace_report.read_text(encoding="utf-8") == "profile report"
    assert delivered == [f"done: {report}\nMEDIA:{workspace_report.resolve()}"]
    assert f"MEDIA:{workspace_report.resolve()}" in delivered[0]

    clear_spike_routes()


@pytest.mark.asyncio
async def test_handle_async_does_not_auto_deliver_sensitive_profile_file_path(monkeypatch, tmp_path):
    """Automatic path delivery must not leak credential or token files."""
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes

    clear_spike_routes()
    profile_home = tmp_path / "profiles" / "sensitive-path-profile"
    profile_home.mkdir(parents=True)
    add_spike_route("ou_sensitive_file", profile_home)

    token_file = profile_home / "feishu_uat" / "ou_user.json"
    token_file.parent.mkdir(parents=True)
    token_file.write_text('{"access_token":"secret"}', encoding="utf-8")

    async def fake_enrich(event, gateway):
        return "show token path"

    async def fake_stream(adapter, chat_id, profile_name, profile_home, event, *, messages=None):
        return f"debug file: {token_file}"

    class FullFeishuAdapter:
        async def on_processing_start(self, event):
            return None

        async def on_processing_complete(self, event, outcome):
            return None

        async def edit_message(self, *args, **kwargs):
            return None

    delivered = []

    async def deliver_media(response, delivered_event, adapter):
        delivered.append(response)

    monkeypatch.setattr(router_mod, "_enrich_via_hermes_pipeline", fake_enrich)
    monkeypatch.setattr(router_mod, "_stream_into_feishu", fake_stream)

    gateway = SimpleNamespace(
        adapters={"feishu": FullFeishuAdapter()},
        _deliver_media_from_response=deliver_media,
    )

    await router_mod.handle_async(event=_build_event(text="show token path", user_id="ou_sensitive_file"), gateway=gateway)

    assert delivered == []
    assert not (profile_home / "workspace" / "Downloads" / token_file.name).exists()

    clear_spike_routes()


def test_plain_profile_file_paths_are_hidden_from_visible_text(tmp_path):
    """Visible card text should not expose host paths that will be sent as docs."""
    from hermes_multitenancy import router as router_mod

    profile_home = tmp_path / "profiles" / "visible-path-profile"
    report = profile_home / ".ai-docs" / "report.md"
    report.parent.mkdir(parents=True)
    report.write_text("report", encoding="utf-8")

    visible = router_mod._clean_stream_display_text(f"saved at {report}", profile_home)

    assert str(report) not in visible
    assert "[Markdown 源文件已自动发送]" in visible
    assert (profile_home / "workspace" / "Downloads" / "report.md").read_text(encoding="utf-8") == "report"


def test_feishu_document_id_is_rendered_as_link_when_tenant_base_url_configured(monkeypatch):
    """Skill output can hand users a clickable doc URL for configured tenants."""
    from hermes_multitenancy import router as router_mod

    monkeypatch.setenv("HERMES_FEISHU_DOCUMENT_BASE_URL", "https://docs.example.feishu.cn")
    visible = router_mod._clean_stream_display_text(
        "📄 文档：260 9.0 voc反馈优化（平台部分）\n"
        "🆔 飞书文档ID：EOWEwIt58iEhR8kaSkycakM3nge"
    )

    assert "飞书文档ID" not in visible
    assert "https://docs.example.feishu.cn/docx/EOWEwIt58iEhR8kaSkycakM3nge" in visible


def test_feishu_document_id_uses_public_feishu_domain_without_tenant_base_url(monkeypatch):
    """Open-source defaults use Feishu's public URL, not a company tenant domain."""
    from hermes_multitenancy import router as router_mod

    monkeypatch.delenv("HERMES_FEISHU_DOCUMENT_BASE_URL", raising=False)
    visible = router_mod._clean_stream_display_text("🆔 飞书文档ID：EOWEwIt58iEhR8kaSkycakM3nge")

    assert visible == "🔗 飞书文档链接：https://feishu.cn/docx/EOWEwIt58iEhR8kaSkycakM3nge"


def test_feishu_wiki_token_is_rendered_as_wiki_link(monkeypatch):
    """Wiki-space tokens should keep the /wiki/ route instead of /docx/."""
    from hermes_multitenancy import router as router_mod

    monkeypatch.delenv("HERMES_FEISHU_DOCUMENT_BASE_URL", raising=False)
    visible = router_mod._clean_stream_display_text("wiki token：QHwGwy1cJidhf2k6oyjcmgLdnAh")

    assert visible == "🔗 飞书 Wiki 链接：https://feishu.cn/wiki/QHwGwy1cJidhf2k6oyjcmgLdnAh"


def test_plain_profile_markdown_paths_are_hidden_from_visible_text(tmp_path):
    """Both .md and .markdown source files are delivered without exposing host paths."""
    from hermes_multitenancy import router as router_mod

    profile_home = tmp_path / "profiles" / "visible-markdown-profile"
    report = profile_home / ".ai-docs" / "report.markdown"
    report.parent.mkdir(parents=True)
    report.write_text("report", encoding="utf-8")

    visible = router_mod._clean_stream_display_text(f"saved at {report}", profile_home)
    response = router_mod._append_profile_file_media_directives(f"saved at {report}", profile_home)
    workspace_report = profile_home / "workspace" / "Downloads" / "report.markdown"

    assert str(report) not in visible
    assert "[Markdown 源文件已自动发送]" in visible
    assert workspace_report.read_text(encoding="utf-8") == "report"
    assert f"MEDIA:{workspace_report.resolve()}" in response


@pytest.mark.asyncio
async def test_post_stream_media_delivery_sends_chinese_markdown_filename_directly(tmp_path):
    """Upstream MEDIA extraction misses Chinese .md filenames; multitenancy must not."""
    from hermes_multitenancy import router as router_mod

    profile_home = tmp_path / "profiles" / "owner"
    source = profile_home / ".ai-docs" / "技术方案_260.md"
    source.parent.mkdir(parents=True)
    source.write_text("report", encoding="utf-8")
    event = _build_event(chat_id="oc_chat")
    event.source.chat_type = "group"
    event.message_id = "om_source_doc"

    class Adapter:
        def __init__(self):
            self.documents = []

        async def send_document(self, **kwargs):
            self.documents.append(kwargs)
            return SimpleNamespace(success=True)

    adapter = Adapter()
    gateway = SimpleNamespace()

    await router_mod._deliver_media_from_stream_response(
        gateway,
        f"💾 方案已保存：{source}",
        event,
        adapter,
        profile_home,
    )

    workspace_file = profile_home / "workspace" / "Downloads" / "技术方案_260.md"
    assert workspace_file.read_text(encoding="utf-8") == "report"
    assert adapter.documents == [
        {
            "chat_id": "oc_chat",
            "file_path": str(workspace_file.resolve()),
            "file_name": "技术方案_260.md",
            "reply_to": "om_source_doc",
            "metadata": None,
        }
    ]


def test_post_stream_media_delivery_sends_public_markdown_image_url(tmp_path, monkeypatch):
    """Public markdown image URLs must become Feishu image uploads, not stripped card text."""
    import socket
    import urllib.request

    from hermes_multitenancy import router as router_mod

    profile_home = tmp_path / "profiles" / "owner"
    profile_home.mkdir(parents=True)
    event = _build_event(chat_id="oc_chat")
    event.message_id = "om_vod_image"
    payload = b"\xff\xd8\xff\xe0fake-vod-jpeg"
    calls = []

    class FakeHTTPResponse:
        _hermes_peer_ip = "93.184.216.34"
        headers = {
            "Content-Type": "image/jpeg",
            "Content-Length": str(len(payload)),
        }

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, size=-1):
            return payload

    def fake_urlopen(request, timeout=0):
        calls.append((request.full_url, timeout))
        return FakeHTTPResponse()

    def fake_getaddrinfo(host, port, *args, **kwargs):
        assert host == "cdn.example.com"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    class Adapter:
        def __init__(self):
            self.images = []

        async def send_image_file(self, **kwargs):
            self.images.append(kwargs)
            return SimpleNamespace(success=True)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    adapter = Adapter()

    asyncio.run(
        router_mod._deliver_media_from_stream_response(
            SimpleNamespace(),
            "海报生成成功\n![未来城市夜景海报](https://cdn.example.com/path/aigcImageGenFile.jpg)",
            event,
            adapter,
            profile_home,
        )
    )

    assert calls == [("https://cdn.example.com/path/aigcImageGenFile.jpg", 15)]
    assert len(adapter.images) == 1
    image_path = Path(adapter.images[0]["image_path"])
    assert image_path.parent == profile_home / "workspace" / "Downloads" / "remote-images"
    assert image_path.suffix == ".jpg"
    assert image_path.read_bytes() == payload
    # issue #1 (openclaw parity): media/image delivery now quotes the original
    # request message (reply_in_thread=False => ordinary quote, not a topic).
    assert adapter.images[0] == {
        "chat_id": "oc_chat",
        "image_path": str(image_path),
        "reply_to": "om_vod_image",
        "metadata": None,
    }


def test_post_stream_remote_image_materialization_runs_off_event_loop(tmp_path, monkeypatch):
    """Blocking remote image materialization must not run on the gateway loop thread."""
    import threading

    from hermes_multitenancy import router as router_mod

    profile_home = tmp_path / "profiles" / "owner"
    profile_home.mkdir(parents=True)
    event = _build_event(chat_id="oc_chat")
    loop_thread = threading.get_ident()
    materialize_threads: list[int] = []

    def fake_materialize(url: str, materialize_profile_home: Path):
        assert url == "https://cdn.example.com/path/aigcImageGenFile.jpg"
        assert materialize_profile_home == profile_home
        materialize_threads.append(threading.get_ident())
        target = profile_home / "workspace" / "Downloads" / "remote-images" / "poster.jpg"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"jpeg")
        return target

    class Adapter:
        def __init__(self):
            self.images = []

        async def send_image_file(self, **kwargs):
            self.images.append(kwargs)
            return SimpleNamespace(success=True)

    monkeypatch.setattr(router_mod, "_materialize_remote_image_url", fake_materialize)
    adapter = Adapter()

    asyncio.run(
        router_mod._deliver_media_from_stream_response(
            SimpleNamespace(),
            "海报生成成功\n![未来城市夜景海报](https://cdn.example.com/path/aigcImageGenFile.jpg)",
            event,
            adapter,
            profile_home,
        )
    )

    assert materialize_threads
    assert all(thread_id != loop_thread for thread_id in materialize_threads)
    assert len(adapter.images) == 1


def test_post_stream_media_delivery_rejects_private_markdown_image_url(tmp_path, monkeypatch):
    """Remote image delivery must not turn model output into an SSRF fetch."""
    import socket
    import urllib.request

    from hermes_multitenancy import router as router_mod

    profile_home = tmp_path / "profiles" / "owner"
    profile_home.mkdir(parents=True)
    event = _build_event(chat_id="oc_chat")

    def fake_getaddrinfo(host, port, *args, **kwargs):
        assert host == "internal.example.com"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    def fail_urlopen(*args, **kwargs):
        raise AssertionError("private remote image URL should not be fetched")

    class Adapter:
        def __init__(self):
            self.images = []

        async def send_image_file(self, **kwargs):
            self.images.append(kwargs)
            return SimpleNamespace(success=True)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(urllib.request, "urlopen", fail_urlopen)
    adapter = Adapter()

    asyncio.run(
        router_mod._deliver_media_from_stream_response(
            SimpleNamespace(),
            "海报生成成功\n![未来城市夜景海报](http://internal.example.com/path/aigcImageGenFile.jpg)",
            event,
            adapter,
            profile_home,
        )
    )

    assert adapter.images == []
    assert not (profile_home / "workspace" / "Downloads" / "remote-images").exists()


def test_post_stream_media_delivery_rejects_rebound_private_peer_ip(tmp_path, monkeypatch):
    """A host that resolves public but connects private must not be fetched as media."""
    import socket
    import urllib.request

    from hermes_multitenancy import router as router_mod

    profile_home = tmp_path / "profiles" / "owner"
    profile_home.mkdir(parents=True)
    event = _build_event(chat_id="oc_chat")

    class FakeHTTPResponse:
        _hermes_peer_ip = "127.0.0.1"
        headers = {
            "Content-Type": "image/jpeg",
            "Content-Length": "4",
        }

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, size=-1):  # pragma: no cover - peer check should stop first
            raise AssertionError("private peer response body should not be read")

    def fake_getaddrinfo(host, port, *args, **kwargs):
        assert host == "rebind.example.com"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    def fake_urlopen(request, timeout=0):
        return FakeHTTPResponse()

    class Adapter:
        def __init__(self):
            self.images = []

        async def send_image_file(self, **kwargs):
            self.images.append(kwargs)
            return SimpleNamespace(success=True)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    adapter = Adapter()

    asyncio.run(
        router_mod._deliver_media_from_stream_response(
            SimpleNamespace(),
            "海报生成成功\n![未来城市夜景海报](http://rebind.example.com/path/aigcImageGenFile.jpg)",
            event,
            adapter,
            profile_home,
        )
    )

    assert adapter.images == []
    assert not (profile_home / "workspace" / "Downloads" / "remote-images").exists()


def test_webui_profile_scoped_media_response_materializes_public_markdown_image_url(tmp_path, monkeypatch):
    """WebUI should receive profile workspace media instead of relying on remote image markdown."""
    import socket
    import urllib.request

    from hermes_multitenancy import router as router_mod

    profile_home = tmp_path / "profiles" / "owner"
    profile_home.mkdir(parents=True)
    payload = b"\x89PNG\r\n\x1a\nfake-remote-png"

    class FakeHTTPResponse:
        _hermes_peer_ip = "93.184.216.34"
        headers = {
            "Content-Type": "image/png",
            "Content-Length": str(len(payload)),
        }

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, size=-1):
            return payload

    def fake_getaddrinfo(host, port, *args, **kwargs):
        assert host == "images.example.com"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    def fake_urlopen(request, timeout=0):
        return FakeHTTPResponse()

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    response = router_mod._webui_profile_scoped_media_response(
        "海报生成成功\n![poster](https://images.example.com/generated/poster.png)",
        profile_home,
    )

    media_lines = [line for line in response.splitlines() if line.startswith("MEDIA:")]
    assert len(media_lines) == 1
    assert media_lines[0].startswith("MEDIA:/workspace/Downloads/remote-images/")
    assert media_lines[0].endswith(".png")
    workspace_relative = media_lines[0].removeprefix("MEDIA:/workspace/")
    assert (profile_home / "workspace" / workspace_relative).read_bytes() == payload


def test_recent_profile_markdown_file_context_is_added_for_this_file_request(tmp_path):
    from hermes_multitenancy import router as router_mod

    profile_home = tmp_path / "profiles" / "owner"
    report = profile_home / ".ai-docs" / "kep-prd-analysis" / "技术方案_260.md"
    report.parent.mkdir(parents=True)
    report.write_text("report", encoding="utf-8")

    prior = [
        {
            "role": "assistant",
            "content": f"💾 方案已保存：`{report}`\n[Markdown 源文件已自动发送]",
        }
    ]

    text = router_mod._append_recent_profile_file_context(
        "该文件转成飞书云文档发给我",
        profile_name="owner",
        chat_id="oc_chat",
        profile_home=profile_home,
        prior_messages=prior,
    )

    workspace_file = profile_home / "workspace" / "Downloads" / "技术方案_260.md"
    assert workspace_file.read_text(encoding="utf-8") == "report"
    assert "最近 Hermes 已投递给当前会话的文件" in text
    assert "workspace_path: /workspace/Downloads/技术方案_260.md" in text
    assert str(report) in text


@pytest.mark.asyncio
async def test_handle_async_blocks_explicit_media_directive_for_sensitive_profile_file(monkeypatch, tmp_path):
    """Explicit MEDIA directives must not bypass sensitive profile file filters."""
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes

    clear_spike_routes()
    profile_home = tmp_path / "profiles" / "explicit-sensitive-profile"
    profile_home.mkdir(parents=True)
    add_spike_route("ou_explicit_sensitive_file", profile_home)

    env_file = profile_home / ".env"
    env_file.write_text("SECRET=value", encoding="utf-8")

    async def fake_enrich(event, gateway):
        return "send env file"

    async def fake_stream(adapter, chat_id, profile_name, profile_home, event, *, messages=None):
        return f"created\nMEDIA:{env_file}"

    class FullFeishuAdapter:
        async def on_processing_start(self, event):
            return None

        async def on_processing_complete(self, event, outcome):
            return None

        async def edit_message(self, *args, **kwargs):
            return None

    delivered = []

    async def deliver_media(response, delivered_event, adapter):
        delivered.append(response)

    monkeypatch.setattr(router_mod, "_enrich_via_hermes_pipeline", fake_enrich)
    monkeypatch.setattr(router_mod, "_stream_into_feishu", fake_stream)

    gateway = SimpleNamespace(
        adapters={"feishu": FullFeishuAdapter()},
        _deliver_media_from_response=deliver_media,
    )

    await router_mod.handle_async(
        event=_build_event(text="send env file", user_id="ou_explicit_sensitive_file"),
        gateway=gateway,
    )

    assert delivered == []

    clear_spike_routes()


@pytest.mark.asyncio
async def test_handle_async_blocks_outbound_media_outside_profile(monkeypatch, tmp_path):
    """A tenant must not be able to attach files outside its routed profile home."""
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes

    clear_spike_routes()
    profile_home = tmp_path / "profiles" / "media-profile"
    profile_home.mkdir(parents=True)
    add_spike_route("ou_media_block", profile_home)

    outside_path = tmp_path / "other-profile" / "secret.md"
    outside_path.parent.mkdir()
    outside_path.write_text("do not send", encoding="utf-8")

    async def fake_enrich(event, gateway):
        return "make a file"

    async def fake_stream(adapter, chat_id, profile_name, profile_home, event, *, messages=None):
        return f"created\nMEDIA:{outside_path}"

    class FullFeishuAdapter:
        async def on_processing_start(self, event):
            return None

        async def on_processing_complete(self, event, outcome):
            return None

        async def edit_message(self, *args, **kwargs):
            return None

    delivered = []

    async def deliver_media(response, delivered_event, adapter):
        delivered.append(response)

    monkeypatch.setattr(router_mod, "_enrich_via_hermes_pipeline", fake_enrich)
    monkeypatch.setattr(router_mod, "_stream_into_feishu", fake_stream)

    gateway = SimpleNamespace(
        adapters={"feishu": FullFeishuAdapter()},
        _deliver_media_from_response=deliver_media,
    )

    await router_mod.handle_async(event=_build_event(text="make a file", user_id="ou_media_block"), gateway=gateway)

    assert delivered == []

    clear_spike_routes()


@pytest.mark.asyncio
async def test_concurrent_uploaded_files_keep_profile_and_prompt_isolated(monkeypatch, tmp_path):
    """Same-named uploads from different users must not cross profile or prompt state."""
    from pathlib import Path

    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.runtime import add_spike_route, clear_spike_routes

    clear_spike_routes()
    router_mod._user_inflight_tasks.clear()

    profile_a = tmp_path / "profiles" / "alice"
    profile_b = tmp_path / "profiles" / "bob"
    profile_a.mkdir(parents=True)
    profile_b.mkdir(parents=True)
    add_spike_route("ou_file_a", profile_a)
    add_spike_route("ou_file_b", profile_b)

    upload_a = tmp_path / "uploads" / "a" / "same-name.md"
    upload_b = tmp_path / "uploads" / "b" / "same-name.md"
    upload_a.parent.mkdir(parents=True)
    upload_b.parent.mkdir(parents=True)
    upload_a.write_text("MARKER_FILE_A_ONLY", encoding="utf-8")
    upload_b.write_text("MARKER_FILE_B_ONLY", encoding="utf-8")

    event_a = _build_event(text="", chat_id="chat-a", user_id="ou_file_a")
    event_a.message_id = "om_file_a"
    event_a.media_urls = [str(upload_a)]
    event_a.media_types = ["text/markdown"]
    event_b = _build_event(text="", chat_id="chat-b", user_id="ou_file_b")
    event_b.message_id = "om_file_b"
    event_b.media_urls = [str(upload_b)]
    event_b.media_types = ["text/markdown"]

    async def fake_enrich(event, gateway):
        path = Path(event.media_urls[0])
        await asyncio.sleep(0.01 if event.source.user_id == "ou_file_a" else 0)
        return f"[Content of {path.name}]:\n{path.read_text(encoding='utf-8')}"

    captured = []

    async def fake_stream(adapter, chat_id, profile_name, profile_home, event, *, messages=None):
        captured.append(
            {
                "user_id": event.source.user_id,
                "profile_name": profile_name,
                "profile_home": profile_home,
                "event_text": event.text,
                "user_message": messages[-1]["content"],
            }
        )
        return f"ok-{event.source.user_id}"

    class FullFeishuAdapter:
        async def on_processing_start(self, event):
            return None

        async def on_processing_complete(self, event, outcome):
            return None

        async def edit_message(self, *args, **kwargs):
            return None

    monkeypatch.setattr(router_mod, "_enrich_via_hermes_pipeline", fake_enrich)
    monkeypatch.setattr(router_mod, "_stream_into_feishu", fake_stream)

    gateway = SimpleNamespace(adapters={"feishu": FullFeishuAdapter()})

    await asyncio.gather(
        router_mod.handle_async(event=event_a, gateway=gateway),
        router_mod.handle_async(event=event_b, gateway=gateway),
    )

    by_user = {item["user_id"]: item for item in captured}
    assert by_user["ou_file_a"]["profile_home"] == profile_a
    assert by_user["ou_file_b"]["profile_home"] == profile_b
    assert "MARKER_FILE_A_ONLY" in by_user["ou_file_a"]["event_text"]
    assert "MARKER_FILE_B_ONLY" not in by_user["ou_file_a"]["event_text"]
    assert "MARKER_FILE_B_ONLY" in by_user["ou_file_b"]["user_message"]
    assert "MARKER_FILE_A_ONLY" not in by_user["ou_file_b"]["user_message"]

    clear_spike_routes()
