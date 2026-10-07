"""Finite tenant runs relay child identities and reap their worker on stop."""
import asyncio
import sys
from types import SimpleNamespace

import pytest

from tests.test_aiagent_subprocess import _event, _install_fake_feishu_oapi


def test_child_events_keep_distinct_ids_and_exclude_private_payload(monkeypatch, tmp_path):
    from hermes_multitenancy import agent_real
    profile = tmp_path / 'profiles' / 'owner'
    profile.mkdir(parents=True)
    (profile / 'config.yaml').write_text('model:\n  default: openai/test-model\n')
    (profile / '.env').write_text('OPENAI_API_KEY=test-key\n')

    class Agent:
        def __init__(self, **kwargs):
            self.callback = kwargs['tool_progress_callback']

        def run_conversation(self, **kwargs):
            for child in ('child-a', 'child-b'):
                for event in ('subagent.start', 'subagent.tool', 'subagent.progress', 'subagent.complete'):
                    self.callback(event, 'terminal', 'PRIVATE', {'secret': 'PRIVATE'},
                                  subagent_id=child, goal='same goal' * 200, status='completed',
                                  child_session_id=child + '-session', tool_count=2,
                                  summary='PRIVATE', token='PRIVATE')
            self.callback('subagent.thinking', None, 'PRIVATE', subagent_id='child-a')
            self.callback('subagent.start', None, None, goal='no identity')
            self.callback('subagent.start', None, None, subagent_id='x' * 201)
            return {'final_response': 'done'}

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, 'run_agent', SimpleNamespace(AIAgent=Agent))
    _install_fake_feishu_oapi(monkeypatch)
    events = []
    assert agent_real._run_with_aiagent(
        _event(), profile, event_sink=lambda kind, **p: events.append((kind, p))) == 'done'
    assert len(events) == 8
    assert {p['subagent_id'] for _, p in events} == {'child-a', 'child-b'}
    assert all(p['session_id'] and len(p['goal']) == 1000 for _, p in events)
    assert 'PRIVATE' not in str(events)


@pytest.mark.asyncio
async def test_cancel_reaps_real_worker_with_child_thread(monkeypatch, tmp_path):
    from hermes_multitenancy import agent_real
    started = asyncio.Event()
    original_spawn = asyncio.create_subprocess_exec
    children = []

    async def spawn(*args, **kwargs):
        proc = await original_spawn(
            sys.executable, '-u', '-c',
            'import threading,time; threading.Thread(target=lambda: time.sleep(60), daemon=True).start(); print("ready",flush=True); time.sleep(60)',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        children.append(proc)
        assert await proc.stdout.readline() == b'ready\n'
        started.set()
        return proc

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    task = asyncio.create_task(agent_real._run_aiagent_subprocess(_event(), tmp_path))
    try:
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert children[0].returncode is not None
    finally:
        for proc in children:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()


@pytest.mark.asyncio
@pytest.mark.parametrize('incomplete', [False, True])
async def test_run_result_completion_flag(monkeypatch, tmp_path, incomplete):
    from hermes_multitenancy.run_broker import RunBroker, mark_current_run_output_incomplete
    from hermes_multitenancy.run_models import RunRequest
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))

    def dispatch(request):
        if incomplete:
            mark_current_run_output_incomplete()
        return 'partial' if incomplete else 'complete'

    result = await RunBroker(dispatch_agent=dispatch).run(
        RunRequest(channel='webui', profile_name='owner', user_key='actor', content='test'))
    assert result.completed is not incomplete


@pytest.mark.asyncio
async def test_dispatch_relays_children_with_trusted_run_and_session(monkeypatch, tmp_path):
    from hermes_multitenancy import agent_real, router
    from hermes_multitenancy.run_models import RunRequest
    from hermes_multitenancy.webui_broker_server import _default_dispatch_agent

    async def stream(*args, **kwargs):
        for kind in ('subagent.start', 'subagent.tool', 'subagent.progress', 'subagent.complete'):
            yield kind, {'subagent_id': 'child-a', 'session_id': 'untrusted', 'broker_run_id': 'untrusted'}
        yield 'content', 'done'

    monkeypatch.setattr(agent_real, 'stream_run_agent', stream)
    monkeypatch.setattr(router, '_profile_name_to_home', lambda p: tmp_path / 'profiles' / p)
    events = []

    async def emit(event):
        events.append(event)

    await _default_dispatch_agent(
        RunRequest(channel='webui', profile_name='owner', user_key='actor', content='test', session_id='trusted-session'),
        emit_event=emit, auth_signal_run_id='trusted-run')
    children = [e for e in events if e.kind.startswith('subagent.')]
    assert len(children) == 4
    assert all(e.payload['broker_run_id'] == 'trusted-run' and e.payload['session_id'] == 'trusted-session' for e in children)


@pytest.mark.asyncio
async def test_subprocess_stream_preserves_child_lifecycle(monkeypatch, tmp_path):
    import json
    from hermes_multitenancy import agent_real
    original_spawn = asyncio.create_subprocess_exec
    profile = tmp_path / 'profiles' / 'owner'
    profile.mkdir(parents=True)
    frames = [{'event': kind, 'subagent_id': 'child-a', 'tool_count': 1} for kind in
              ('subagent.start', 'subagent.tool', 'subagent.progress', 'subagent.complete')]
    frames.append({'event': 'done', 'result': 'done', 'error': None})
    script = 'import sys; sys.stdin.buffer.read(); print(' + repr('\n'.join(json.dumps(f) for f in frames)) + ', flush=True)'

    async def spawn(*args, **kwargs):
        return await original_spawn(sys.executable, '-u', '-c', script,
                                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                    stderr=asyncio.subprocess.PIPE)

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setenv('HERMES_AIAGENT_WARM_WORKER', '0')
    events = [item async for item in agent_real._stream_aiagent_subprocess(_event(), profile)]
    assert [kind for kind, _ in events] == [frame['event'] for frame in frames]
    assert all(payload['subagent_id'] == 'child-a' for _, payload in events[:-1])


@pytest.mark.asyncio
@pytest.mark.parametrize('before_admission', [False, True])
async def test_http_cancel_is_bound_to_owner_profile_session_and_exact_run(monkeypatch, tmp_path, before_admission):
    import json
    from aiohttp.test_utils import TestClient, TestServer
    from hermes_multitenancy import router, webui_broker_server as server
    from hermes_multitenancy.routing import RoutingTable

    table = RoutingTable(tmp_path / 'routing.db')
    table.upsert(user_id='test-owner', profile_name='owner', open_id='actor', provenance='sync')
    table.close()
    router.override_routing_table(tmp_path / "routing.db")
    monkeypatch.setattr(server, '_run_broker_key', lambda: 'test-broker-key')
    started = asyncio.Event()
    cleaned = asyncio.Event()

    async def wait_until_stopped():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    async def dispatch(request):
        assert not before_admission, "cancelled admission must never dispatch"
        await wait_until_stopped()

    if before_admission:
        from hermes_multitenancy import billing_identity

        async def prepare(request, **kwargs):
            await wait_until_stopped()
            return request

        monkeypatch.setattr(billing_identity, 'prepare_billing_request', prepare)

    app = server.create_run_broker_app(dispatch_agent=dispatch, mark_seen=lambda _: True,
                                      sandbox_available=lambda: True)
    client = TestClient(TestServer(app))
    await client.start_server()
    headers = {'Authorization': 'Bearer test-broker-key', 'X-Hermes-Owner-Open-Id': 'actor'}
    body = {'profile_name': 'owner', 'session_id': 'session-a'}
    response = None
    try:
        response = await client.post('/api/run-broker/runs', headers=headers, json={
            **body, 'channel': 'webui', 'user_key': 'actor', 'content': 'test'})
        assert response.status == 200, await response.text()
        frame = json.loads((await response.content.readline()).decode().removeprefix('data: '))
        assert frame['kind'] == 'run_started'
        run_id = frame['payload']['broker_run_id']
        await asyncio.wait_for(started.wait(), 5)
        url = '/api/run-broker/runs/' + run_id + '/cancel'
        for bad_headers, bad_body, expected in (
            ({}, body, 401),
            ({'Authorization': 'Bearer test-broker-key'}, body, 403),
            ({**headers, 'X-Hermes-Owner-Open-Id': 'other'}, body, 404),
            (headers, {**body, 'profile_name': 'other'}, 404),
            (headers, {**body, 'session_id': 'other'}, 404),
        ):
            denied = await client.post(url, headers=bad_headers, json=bad_body)
            assert denied.status == expected
            assert not cleaned.is_set()
        stopped = await client.post(url, headers=headers, json=body)
        assert stopped.status == 200
        assert await stopped.json() == {'ok': True, 'cancelled': True}
        assert cleaned.is_set()
        stale = await client.post(url, headers=headers, json=body)
        assert stale.status == 404
        assert '"cancelled": true' in await response.text()
    finally:
        if response:
            response.close()
        await client.close()
        router.override_routing_table(None)
