"""release-bundle.sh 生成路径的验收测试(评审 #p1 补齐)。

fixture 双仓 → release-cut 打标 → release-bundle 产包,校验清单/归档/compose/
digest 钉死/README/tar 全链。Docker 实跑(S4)按 SPEC 留在 SIM,收据在
.ftask/release-manifest-packaging/SIM_TRACE.md。
"""

import os
import subprocess
import time
from pathlib import Path

import pytest

from tests.test_release_cut import _cut, _git, _latest_tag, _make_repo

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
FAKE_DIGEST = "nousresearch/hermes-agent@sha256:" + "ab" * 32


@pytest.fixture()
def bundle(tmp_path):
    mt = _make_repo(tmp_path, "mt")
    webui = _make_repo(tmp_path, "webui")
    # bundle 的前提是 webui 源码里有带 ARG BASE_IMAGE 的 Dockerfile
    (webui / "Dockerfile").write_text(
        "ARG BASE_IMAGE=nousresearch/hermes-agent:latest\nFROM ${BASE_IMAGE}\n"
    )
    _git(webui, "add", "-A")
    _git(webui, "commit", "-q", "-m", "dockerfile")
    _git(webui, "push", "-q", "origin", "main")
    assert _cut(mt, webui).returncode == 0
    tag = _latest_tag(mt)

    out = tmp_path / "out"
    r = subprocess.run(
        ["bash", str(DEPLOY / "release-bundle.sh"), tag,
         "--repo-mt", str(mt), "--repo-webui", str(webui), "--out", str(out)],
        capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
             "HOME": str(tmp_path),
             "RELEASE_BUNDLE_BASE_DIGEST": FAKE_DIGEST},
    )
    assert r.returncode == 0, r.stdout + r.stderr
    return mt, webui, tag, out


def test_bundle_layout_and_version_manifest(bundle):
    mt, webui, tag, out = bundle
    version = (out / "VERSION").read_text()
    assert f"tag={tag}" in version
    assert f"multitenancy={_git(mt, 'rev-parse', 'origin/main')}" in version
    assert f"webui={_git(webui, 'rev-parse', 'origin/main')}" in version
    assert f"base_image={FAKE_DIGEST}" in version
    # 双仓源码归档真实落盘
    assert (out / "webui" / "f.txt").is_file()
    assert (out / "webui" / "vendor" / "hermes-multitenancy" / "f.txt").is_file()
    assert out.with_suffix(".tar.gz").is_file() or Path(str(out) + ".tar.gz").is_file()


def test_bundle_dockerfile_overlay_and_pinned_compose(bundle):
    _, _, tag, out = bundle
    dockerfile = (out / "webui" / "Dockerfile.bundle").read_text()
    # ARG 默认值必须改写为不可变 digest:绕过 compose 手动 docker build 也不能漂 latest
    assert dockerfile.startswith(f"ARG BASE_IMAGE={FAKE_DIGEST}\n")
    assert "COPY vendor/hermes-multitenancy" in dockerfile   # 插件钉入层
    assert "uv pip install --python" in dockerfile
    compose = (out / "docker-compose.yml").read_text()
    assert "dockerfile: Dockerfile.bundle" in compose
    assert f"BASE_IMAGE: {FAKE_DIGEST}" in compose           # base 镜像钉 digest
    assert f"image: hermes-bundle:{tag}" in compose
    readme = (out / "README.md").read_text()
    assert "## 安装" in readme and "## 回滚" in readme


def test_bundle_refuses_mutable_base_ref(bundle, tmp_path):
    mt, webui, _, _ = bundle
    r = subprocess.run(
        ["bash", str(DEPLOY / "release-bundle.sh"), _latest_tag(mt),
         "--repo-mt", str(mt), "--repo-webui", str(webui), "--out", str(tmp_path / "m")],
        capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
             "RELEASE_BUNDLE_BASE_DIGEST": "nousresearch/hermes-agent:latest"},
    )
    assert r.returncode != 0 and "不可变 digest" in r.stdout + r.stderr
    assert not (tmp_path / "m").exists()


@pytest.mark.skipif(
    not os.environ.get("RELEASE_BUNDLE_DOCKER_E2E"),
    reason="set RELEASE_BUNDLE_DOCKER_E2E=1 — 需要 Docker+真实双仓,从 tar 全新构建约 10 分钟",
)
def test_bundle_docker_e2e_from_tar(tmp_path):
    """S4 终局收据:交付 tar 在干净目录解包 → compose 从零 build/up → 200 + 插件可导入。"""
    mt = Path.home() / "code" / "hermes-multitenancy"
    webui = Path.home() / "code" / "hermes-web-ui"
    tag = _latest_tag(mt)
    out = tmp_path / f"hermes-bundle-{tag}"
    r = subprocess.run(
        ["bash", str(DEPLOY / "release-bundle.sh"), tag,
         "--repo-mt", str(mt), "--repo-webui", str(webui), "--out", str(out)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    tar = Path(str(out) + ".tar.gz")
    assert tar.is_file()

    clean = tmp_path / "clean"
    clean.mkdir()
    subprocess.run(["tar", "-xzf", str(tar), "-C", str(clean)], check=True)
    proj = clean / f"hermes-bundle-{tag}"
    (proj / ".env").write_text(
        "PORT=6060\nHERMES_DATA_DIR=./hermes_data\n"
        "WEBUI_CONTAINER_NAME=hermes-bundle-e2e\n"
        "PREVIEW_FRONTEND_PORT=8651\nXAI_OAUTH_PORT=56122\n"
    )
    try:
        up = subprocess.run(
            ["docker", "compose", "up", "-d", "--build"],
            cwd=proj, capture_output=True, text=True, timeout=1800,
        )
        assert up.returncode == 0, up.stdout[-2000:] + up.stderr[-2000:]
        for _ in range(60):
            ok = subprocess.run(
                ["curl", "-fsS", "-o", "/dev/null", "http://localhost:6060/"],
                capture_output=True,
            ).returncode == 0
            if ok:
                break
            time.sleep(3)
        assert ok, "webui 3 分钟内未返回 200"
        imp = subprocess.run(
            ["docker", "exec", "hermes-bundle-e2e",
             "/opt/hermes/.venv/bin/python", "-c", "import hermes_multitenancy._import_smoke"],
            capture_output=True, text=True,
        )
        assert imp.returncode == 0, imp.stderr
    finally:
        subprocess.run(["docker", "compose", "down"], cwd=proj, capture_output=True)


def test_bundle_refuses_missing_tag_and_existing_out(bundle, tmp_path):
    mt, webui, _, out = bundle
    r = subprocess.run(
        ["bash", str(DEPLOY / "release-bundle.sh"), "release-19700101-01",
         "--repo-mt", str(mt), "--repo-webui", str(webui), "--out", str(tmp_path / "x")],
        capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
             "RELEASE_BUNDLE_BASE_DIGEST": FAKE_DIGEST},
    )
    assert r.returncode != 0 and "标签不存在" in r.stdout + r.stderr
    r = subprocess.run(
        ["bash", str(DEPLOY / "release-bundle.sh"), _latest_tag(mt),
         "--repo-mt", str(mt), "--repo-webui", str(webui), "--out", str(out)],
        capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
             "RELEASE_BUNDLE_BASE_DIGEST": FAKE_DIGEST},
    )
    assert r.returncode != 0 and "输出目录已存在" in r.stdout + r.stderr
