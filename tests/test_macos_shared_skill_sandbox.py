"""Real macOS policy checks for installed shared skills."""
from pathlib import Path
import os
import pwd
import subprocess
import sys
import tempfile

import pytest


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS sandbox-exec")
def test_installed_skill_is_read_only_and_does_not_expose_peers(monkeypatch):
    from hermes_multitenancy import agent_real

    node = Path('/opt/homebrew/bin/node')
    if not node.is_file():
        pytest.skip('Node is unavailable')
    with tempfile.TemporaryDirectory(prefix='.hermes-skill-sandbox-', dir=pwd.getpwuid(os.getuid()).pw_dir) as root:
        shared = Path(root).resolve()
        profile = shared / 'profiles' / 'owner'
        skills = profile / 'skills'
        skills.mkdir(parents=True)
        source = shared / 'skill-releases' / '已安装'
        other = shared / 'skill-releases' / 'uninstalled'
        secret = shared / 'skill-releases' / 'secret'
        peer = shared / 'profiles' / 'peer'
        archive = shared / 'skill-releases' / 'archive'
        for folder in (source, other, secret, peer, archive):
            folder.mkdir(parents=True)
            (folder / 'SKILL.md').write_text('# fixture')
            (folder / 'script.js').write_text('module.exports = 42;')
        (secret / '.env').write_text('TEST_FIXTURE=secret')
        (skills / '已安装').symlink_to(source, target_is_directory=True)
        (skills / 'secret').symlink_to(secret, target_is_directory=True)
        (archive / '.archive').mkdir()
        (archive / '.archive' / '.env').write_text('TEST_FIXTURE=secret')
        (skills / 'archive').symlink_to(archive, target_is_directory=True)
        shared_skills = shared / 'skills'
        shared_skills.mkdir()
        (shared_skills / 'escape').symlink_to(peer, target_is_directory=True)
        (skills / 'chain').symlink_to(shared_skills / 'escape' / 'script.js')
        managed = shared / '_managed'
        managed.mkdir()
        (managed / 'aidock-skillhub').symlink_to(peer, target_is_directory=True)
        (skills / 'root-escape').symlink_to(managed / 'aidock-skillhub' / 'script.js')
        monkeypatch.setenv('HERMES_SHARED_HOME', str(shared))
        code = '''const fs=require('fs');
const [good, ...forbidden] = process.argv.slice(1);
if (require(good) !== 42) throw Error('installed source unreadable');
let denied=0;
for (const p of forbidden) {
  try { fs.readFileSync(p); } catch(e) { if (['EPERM','EACCES'].includes(e.code)) { denied++; continue; } throw e; }
}
try { fs.writeFileSync(good, 'changed'); } catch(e) { if (['EPERM','EACCES'].includes(e.code)) denied++; else throw e; }
if (denied !== 7) throw Error('isolation denied count '+denied);
console.log('read=1 denied=7');'''
        cmd = agent_real._wrap_macos_sandbox([
            str(node), '-e', code,
            str(skills / '已安装' / 'script.js'),
            str(other / 'script.js'), str(secret / '.env'), str(peer / 'script.js'),
            str(skills / 'chain'), str(skills / 'root-escape'),
            str(archive / '.archive' / '.env'),
        ], profile)
        assert '-p' in cmd
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == 'read=1 denied=7'
        assert (source / 'script.js').read_text() == 'module.exports = 42;'


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS sandbox-exec")
def test_trusted_node_resolves_and_executes_inside_sandbox(monkeypatch):
    from hermes_multitenancy import agent_real

    node = Path("/opt/homebrew/bin/node")
    if not node.is_file():
        pytest.skip("Homebrew Node is unavailable")
    with tempfile.TemporaryDirectory(prefix=".hermes-node-sandbox-", dir=Path.home()) as root:
        shared = Path(root).resolve()
        profile = shared / "profiles/owner"
        profile.mkdir(parents=True)
        monkeypatch.setenv("HERMES_SHARED_HOME", str(shared))
        code = """import os, subprocess, sys
from hermes_multitenancy.lark_cli_tool import _trusted_script_node
node = _trusted_script_node()
assert node == sys.argv[1], (node, sys.argv[1])
r = subprocess.run([node, '-e', 'console.log(17 + 25)'], capture_output=True, text=True, check=True)
assert r.stdout.strip() == '42'
try:
    os.listdir('/opt')
except PermissionError:
    pass
else:
    raise AssertionError('/opt directory listing was allowed')
print('node_resolved=1 executed=1 opt_listing_denied=1')
"""
        cmd = agent_real._wrap_macos_sandbox([sys.executable, "-c", code, str(node.resolve())], profile)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "node_resolved=1 executed=1 opt_listing_denied=1"
