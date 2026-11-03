"""Real refresh entry: identity rejection must precede provider and vault writes."""
from dataclasses import replace
import json

import pytest

from hermes_multitenancy import billing_employee_key as bek, billing_identity as bi
from hermes_multitenancy.billing_credentials import _ResolvedPayer
from hermes_multitenancy.routing import RoutingTable
from tests.test_billing_employee_key import _manager, _issued, _NOW_MS, _DAY, _metadata


@pytest.fixture
def refresh_case(tmp_path, monkeypatch):
    db = tmp_path / 'routing.db'
    routing = RoutingTable(db)
    routing.upsert(user_id='sunke', profile_name='sunke', open_id='ou_sunke', provenance='sync')
    snapdir = tmp_path / 'org-snapshots'
    snapdir.mkdir()
    snapshot = snapdir / 'org-1.json'
    snapshot.write_text(json.dumps({'employees': {'sunke': {'user_id': 'sunke', 'enterprise_email': 'sunke@example.com'}}, 'departments': []}))
    for key, value in {'HERMES_MULTITENANCY_DB': str(db), 'HERMES_ORG_SNAPSHOT_DIR': str(snapdir), 'HERMES_LITELLM_BILLING_PAYER_IDS': 'sunke', 'HERMES_MULTITENANCY_CREDENTIAL_KEY': 'test-key'}.items():
        monkeypatch.setenv(key, value)
    manager = _manager(tmp_path)
    store = bi.BillingIdentityStore(tmp_path / 'identity.db')
    preparer = bi.BillingIdentityPreparer(routing=routing, store=store, credentials=manager)
    monkeypatch.setattr(bi, '_default_preparer', lambda: preparer)
    payer = _ResolvedPayer('sunke', 'sunke', 'sunke@example.com', '')
    binding = bek.store_binding(preparer, payer, _issued())
    payload = manager._load_payload('sunke', 'sunke')
    payload['expires_at'] = _NOW_MS - 1
    manager._save_payload('sunke', 'sunke', payload)
    store.put(replace(binding, expires_at=_NOW_MS - 1))
    issued_template = _issued()
    calls = {'issue': [], 'store': []}
    def issue(self, **kw):
        calls['issue'].append(kw['employee_id'])
        return replace(issued_template, employee_id=kw['employee_id'], email=kw['enterprise_email'], api_key='sk-' + kw['employee_id'])
    monkeypatch.setattr(bek.EmployeeKeyClient, 'issue', issue)
    original = bek.store_binding
    def write(*args):
        calls['store'].append(args[1].employee_user_id)
        return original(*args)
    monkeypatch.setattr(bek, 'store_binding', write)
    return routing, snapshot, preparer, payer, calls


@pytest.mark.parametrize('fault', ['quarantine', 'email', 'subject', 'vault_profile', 'vault_email', 'tuple', 'ambiguous_profile', 'missing_route', 'missing_snapshot', 'ambiguous_org', 'missing_email', 'unverified', 'blank_binding'])
def test_reject_before_mint_and_preserve_state(refresh_case, fault):
    routing, snapshot, p, payer, calls = refresh_case
    current = p._store.get('sunke')
    if fault in {'quarantine', 'email'}:
        p._store.put(replace(current, **({'profile_name': 'quarantine-sunke'} if fault == 'quarantine' else {'email': 'another@example.com'})))
    elif fault in {'subject', 'vault_profile', 'vault_email', 'tuple'}:
        payload = p._credentials._load_payload('sunke', 'sunke')
        key = {'subject': 'employee_id', 'vault_profile': 'profile_name', 'vault_email': 'enterprise_email', 'tuple': 'litellm_user_id'}[fault]
        payload[key] = 'another'
        p._credentials._save_payload('sunke', 'sunke', payload)
    elif fault == 'blank_binding':
        p._store.put(replace(current, email=''))
    elif fault == 'unverified':
        payload = p._credentials._load_payload('sunke', 'sunke')
        payload['account_identity_verified'] = False
        p._credentials._save_payload('sunke', 'sunke', payload)
    elif fault == 'ambiguous_profile':
        routing.upsert(user_id='another', profile_name='sunke', open_id='ou_another', provenance='sync')
    elif fault == 'missing_route':
        routing._conn.execute("UPDATE multitenancy_routing SET active=0")
        routing._conn.commit()
    elif fault == 'missing_snapshot':
        snapshot.unlink()
    else:
        data = json.loads(snapshot.read_text())
        if fault == 'ambiguous_org':
            data['employees']['another'] = dict(data['employees']['sunke'])
        else:
            del data['employees']['sunke']['enterprise_email']
        snapshot.write_text(json.dumps(data))
    before_binding = p._store.get('sunke')
    before_payload = p._credentials._load_payload('sunke', 'sunke')
    result = bek.run_refresh()
    assert calls == {'issue': [], 'store': []}
    assert result['issued'] == 0
    if result['failure_details']:
        assert result['failure_details'][0]['stage'] == 'needs'
        assert result['failure_details'][0]['reason'] != 'unknown'
    assert p._store.get('sunke') == before_binding
    assert p._credentials._load_payload('sunke', 'sunke') == before_payload


@pytest.mark.parametrize('state', ['expired', 'invalid', 'new'])
def test_two_legal_identities_refresh_without_cross_match(refresh_case, monkeypatch, state):
    routing, snapshot, p, payer, calls = refresh_case
    routing.upsert(user_id='another', profile_name='another', open_id='ou_another', provenance='sync')
    data = json.loads(snapshot.read_text())
    data['employees']['another'] = {'user_id': 'another', 'enterprise_email': 'another@example.com'}
    snapshot.write_text(json.dumps(data))
    monkeypatch.setenv('HERMES_LITELLM_BILLING_PAYER_IDS', 'sunke,another')
    if state == 'invalid':
        payload = p._credentials._load_payload('sunke', 'sunke')
        payload.update(expires_at=_NOW_MS + 10 * _DAY, invalid=True)
        p._credentials._save_payload('sunke', 'sunke', payload)
    elif state == 'new':
        p._credentials._delete_payload('sunke', 'sunke')
    result = bek.run_refresh()
    assert result['issued'] == 2 and result['failed'] == 0
    assert sorted(calls['issue']) == ['another', 'sunke']
    for employee in calls['issue']:
        binding = p._store.get(employee)
        assert binding.employee_user_id == binding.profile_name == employee
        assert p._credentials.runtime_api_key(_metadata(binding)) == 'sk-' + employee
        with pytest.raises(bi.RunRejected):
            p._credentials.runtime_api_key({**_metadata(binding), 'litellm_billing_employee_user_id': 'another' if employee == 'sunke' else 'sunke'})


@pytest.mark.parametrize('fault', ['routing_unavailable', 'unsafe_lock', 'billing_profile_ambiguous'])
def test_dependency_or_identity_unavailable_never_falls_back(refresh_case, fault):
    routing, snapshot, p, payer, calls = refresh_case
    if fault == 'routing_unavailable':
        p._routing = None
    elif fault == 'unsafe_lock':
        from pathlib import Path
        Path(str(Path(routing.db_path).resolve()) + '.identity.lock').chmod(0o666)
    else:
        p._store.put(replace(p._store.get('sunke'), employee_user_id='another'))
    result = bek.run_refresh()
    assert result['failed'] == 1
    assert calls == {'issue': [], 'store': []}


@pytest.mark.parametrize('mutation', ['upsert', 'upsert_group', 'upsert_owned_agent', 'soft_delete'])
def test_identity_fence_blocks_real_cross_process_writers_without_profile_directory(refresh_case, mutation):
    import os
    from pathlib import Path
    import subprocess
    import sys

    routing, snapshot, p, payer, calls = refresh_case
    assert not (Path(routing.db_path).parent / 'profiles' / 'sunke').exists()
    actions = {
        'upsert': "r.upsert(user_id='another', profile_name='sunke', open_id='ou_another', provenance='sync')",
        'upsert_group': "r.upsert_group(chat_id='oc_group', profile_name='sunke', owner_open_id='ou_another')",
        'upsert_owned_agent': "r.upsert_owned_agent(agent_id='agent_other', profile_name='sunke', owner_open_id='ou_another')",
        'soft_delete': "r.soft_delete('sunke')",
    }
    code = '''
import fcntl, os, sys
from hermes_multitenancy.routing import RoutingTable
r = RoutingTable(sys.argv[1])
print('ready', flush=True)
sys.stdin.readline()
fd = os.open(sys.argv[1] + '.identity.lock', os.O_RDWR)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print('fenced', flush=True)
else:
    print('UNPROTECTED', flush=True)
os.close(fd)
exec(sys.argv[2])
print('changed', flush=True)
'''
    process = subprocess.Popen([sys.executable, '-c', code, str(Path(routing.db_path).resolve()), actions[mutation]], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == 'ready'
        with p.refresh_identity(payer):
            process.stdin.write('go\n')
            process.stdin.flush()
            assert process.stdout.readline().strip() == 'fenced'
            import select
            assert not select.select([process.stdout], [], [], 0.15)[0], "identity writer crossed the fence"
            assert routing.resolve_billing_root('sunke', 'sunke') is not None
            # A separate connection can still update heartbeat state while mint holds the fence.
            import sqlite3
            with sqlite3.connect(routing.db_path, timeout=0.1) as conn:
                conn.execute("UPDATE multitenancy_routing SET last_active_at=123 WHERE user_id='sunke'")
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr
        assert stdout.strip() == 'changed'
        assert routing.resolve_billing_root('sunke', 'sunke') is None
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
