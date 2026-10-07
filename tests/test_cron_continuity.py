from types import SimpleNamespace
import sqlite3

import pytest

from hermes_multitenancy import cron_worker as cw
from hermes_multitenancy.cron.continuity import scoped_job, remember_success


@pytest.fixture
def context(tmp_path, monkeypatch):
    from cron import notepad
    home = tmp_path / 'profiles' / 'alice'
    home.mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_USE_SANDBOX', '1')
    monkeypatch.setattr(notepad, 'NOTEPAD_FILE', home / 'cron' / 'notepad.db')
    with sqlite3.connect(tmp_path / 'multitenancy.db') as db:
        db.execute('CREATE TABLE multitenancy_routing (open_id, owner_open_id, kind, profile_name, active)')
        db.execute("INSERT INTO multitenancy_routing VALUES ('ou_alice', NULL, 'user', 'alice', 1)")
    job = dict(id='abcd', prompt='Report new developments.', owner_open_id='ou_alice', owner_profile='alice', context_from=['self'])
    return home, job


def test_two_rounds_use_real_upstream_prompt_and_broker(context, monkeypatch):
    from cron import scheduler
    from hermes_multitenancy import billing_identity
    home, job = context
    prompts = []

    async def prepare(request):
        return request

    async def dispatch(request, profile_home):
        assert profile_home == home
        assert request.session_id == 'cron:abcd'
        prompts.append(request.content)
        return 'FIRST_UNIQUE_RESULT' if len(prompts) == 1 else 'SECOND_UNIQUE_RESULT'

    monkeypatch.setattr(billing_identity, 'prepare_billing_request', prepare)
    monkeypatch.setattr(cw, '_dispatch_cron_request', dispatch)
    first = cw._run_job_through_broker(job, scheduler)
    second = cw._run_job_through_broker(job, scheduler)
    assert first[0] and second[0], (first[3], second[3])
    assert 'FIRST_UNIQUE_RESULT' not in prompts[0]
    assert 'FIRST_UNIQUE_RESULT' in prompts[1]
    assert 'previous_success' in prompts[1]


@pytest.mark.parametrize('content,completed', [('', True), ('  ', True), ('[SILENT]', True), ('broken partial', False)])
def test_failed_empty_silent_do_not_replace_success(context, content, completed):
    from cron import notepad
    home, job = context
    _, scope = scoped_job(job, home)
    remember_success(scope, 'good')
    remember_success(scope, content, completed=completed)
    assert notepad.get_note(scope, 'previous_success') == 'good'


def test_cross_owner_profile_job_and_ambiguous_route(context):
    from cron import notepad
    home, job = context
    _, scope = scoped_job(job, home)
    remember_success(scope, 'private first job')
    _, other = scoped_job({**job, 'id': 'beef'}, home)
    assert notepad.get_note(other, 'previous_success') is None
    for changes in ({'owner_open_id': 'ou_bob'}, {'owner_profile': 'bob'}, {'context_from': ['beef']}):
        with pytest.raises(ValueError):
            scoped_job({**job, **changes}, home)
    with sqlite3.connect(home.parent.parent / 'multitenancy.db') as db:
        db.execute("INSERT INTO multitenancy_routing VALUES ('ou_bob', NULL, 'user', 'alice', 1)")
    with pytest.raises(ValueError, match='ambiguous'):
        scoped_job(job, home)


def test_continuity_api_alias_and_legacy_unchanged(context):
    from tools.cronjob_job_args import _apply_continuity
    home, job = context
    _, scope = scoped_job(job, home)
    assert _apply_continuity(None, True) == ['self']
    assert scoped_job({**job, 'context_from': [], 'continuity': True}, home)[1] == scope
    legacy = {**job, 'context_from': []}
    assert scoped_job(legacy, home) == (legacy, None)


def test_failure_does_not_replace_previous_run(context, monkeypatch):
    from cron import scheduler, notepad
    home, job = context
    _, scope = scoped_job(job, home)
    remember_success(scope, 'good')

    class FailedBroker:
        def __init__(self, **kwargs):
            pass
        async def run(self, request):
            raise RuntimeError('dependency unavailable')

    monkeypatch.setattr(cw, 'RunBroker', FailedBroker)
    result = cw._run_job_through_broker(job, scheduler)
    assert result[0] is False
    assert notepad.get_note(scope, 'previous_success') == 'good'


def test_old_unbound_output_and_notepad_not_injected(context):
    from cron import scheduler, notepad
    home, job = context
    out = home / 'cron' / 'output' / job['id']
    out.mkdir(parents=True)
    (out / 'old.md').write_text('UNTRUSTED_OLD_OWNER_OUTPUT')
    notepad.set_note(job['id'], 'old', 'UNTRUSTED_OLD_OWNER_NOTE')
    prompt_job, _ = scoped_job(job, home)
    prompt = scheduler._build_job_prompt(prompt_job)
    assert 'UNTRUSTED_OLD_OWNER' not in prompt


def test_profile_switch_and_owner_change_cannot_read_previous(context, monkeypatch):
    from cron import notepad, scheduler
    home, job = context
    _, scope = scoped_job(job, home)
    remember_success(scope, 'ALICE_PRIVATE_MARKER')
    with sqlite3.connect(home.parent.parent / 'multitenancy.db') as db:
        db.execute("UPDATE multitenancy_routing SET open_id='ou_bob' WHERE profile_name='alice'")
        db.execute("INSERT INTO multitenancy_routing VALUES ('ou_alice', NULL, 'user', 'second', 1)")
    new_job, new_scope = scoped_job({**job, 'owner_open_id': 'ou_bob'}, home)
    assert new_scope != scope
    assert 'ALICE_PRIVATE_MARKER' not in scheduler._build_job_prompt(new_job)
    other_home = home.parent / 'second'
    other_home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(other_home))
    monkeypatch.setattr(notepad, 'NOTEPAD_FILE', other_home / 'cron' / 'notepad.db')
    other_job, other_scope = scoped_job({**job, 'owner_profile': 'second'}, other_home)
    assert other_scope != scope
    assert 'ALICE_PRIVATE_MARKER' not in scheduler._build_job_prompt(other_job)


def test_reauth_failure_before_dispatch_preserves_context(context, monkeypatch):
    from cron import scheduler, notepad
    from hermes_multitenancy import billing_identity
    home, job = context
    _, scope = scoped_job(job, home)
    remember_success(scope, 'good')

    async def reject(request):
        raise RuntimeError('credential subject unavailable')
    async def unexpected_dispatch(*args):
        pytest.fail('must reject before executing')

    monkeypatch.setattr(billing_identity, 'prepare_billing_request', reject)
    monkeypatch.setattr(cw, '_dispatch_cron_request', unexpected_dispatch)
    assert cw._run_job_through_broker(job, scheduler)[0] is False
    assert notepad.get_note(scope, 'previous_success') == 'good'


def test_bounded_utf8_context(context):
    from cron import notepad
    home, job = context
    _, scope = scoped_job(job, home)
    remember_success(scope, '中文' * 5000)
    saved = notepad.get_note(scope, 'previous_success')
    assert len(saved.encode()) <= 8000
    assert '\ufffd' not in saved


def test_real_broker_incomplete_result_is_failure_and_preserves_context(context, monkeypatch):
    from cron import scheduler, notepad
    from hermes_multitenancy import billing_identity
    from hermes_multitenancy.run_broker import mark_current_run_output_incomplete
    home, job = context
    _, scope = scoped_job(job, home)
    remember_success(scope, 'good')

    async def prepare(request):
        return request
    async def incomplete(request, profile_home):
        mark_current_run_output_incomplete()
        return 'broken partial'

    monkeypatch.setattr(billing_identity, 'prepare_billing_request', prepare)
    monkeypatch.setattr(cw, '_dispatch_cron_request', incomplete)
    result = cw._run_job_through_broker(job, scheduler)
    assert result[0] is False
    assert result[2] == ''
    assert notepad.get_note(scope, 'previous_success') == 'good'


def test_unavailable_notepad_fails_before_dispatch(context, monkeypatch):
    from cron import notepad, scheduler
    home, job = context

    def unavailable(*args):
        raise sqlite3.OperationalError('storage unavailable')
    async def unexpected_dispatch(*args):
        pytest.fail('must reject before executing')

    monkeypatch.setattr(notepad, 'get_note', unavailable)
    monkeypatch.setattr(cw, '_dispatch_cron_request', unexpected_dispatch)
    assert cw._run_job_through_broker(job, scheduler)[0] is False
