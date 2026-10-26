import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from codex_gravity.gui import AntigravityGUI, CDP, GUIError, discover_endpoint, loopback_url


@pytest.mark.parametrize('url', ['https://example.com:443', 'http://192.168.1.2:9222', 'http://0.0.0.0:9222',
                                  'http://localhost', 'http://user:password@localhost:9222',
                                  'http://127.0.0.1:9222/private', 'file:///tmp/socket'])
def test_remote_credentialed_or_ambiguous_endpoints_are_refused(url):
    with pytest.raises(GUIError): loopback_url(url)


def test_endpoint_discovery_reads_only_the_local_port_file(tmp_path, monkeypatch):
    monkeypatch.delenv('ANTIGRAVITY_GUI_CDP_URL', raising=False)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    folder = tmp_path / '.config/Antigravity'; folder.mkdir(parents=True)
    (folder / 'DevToolsActivePort').write_text('36591\n/devtools/browser/id\n')
    assert discover_endpoint() == 'http://127.0.0.1:36591'
    (folder / 'DevToolsActivePort').unlink()
    with pytest.raises(GUIError, match='No CLI fallback'):
        discover_endpoint()


class FakeGUI(AntigravityGUI):
    def __init__(self, root):
        super().__init__('http://127.0.0.1:9222')
        self.current = {'title': 'A conversation - ' + root.name + ' - Antigravity',
                        'url': 'https://127.0.0.1:1234/c/original', 'project': None,
                        'model': 'Gemini 3.8 Flash High', 'busy': False, 'draft_present': False,
                        'user_messages': 1, 'send_enabled': True}
        self.sent = []
        self.new_count = 0
        self.inspection = {'state': 'ready_for_review', 'response': 'A real GUI reply'}
        self.submit_error = None
        self.use_real_inspect = False

    @asynccontextmanager
    async def connect(self, target_id=None):
        yield EvalCDP(self.inspection), {'id': 'main-window'}

    async def snapshot(self, cdp): return dict(self.current)

    async def new_conversation(self, cdp):
        self.new_count += 1
        self.current.update(url='https://127.0.0.1:1234/c/fresh-new', title='Antigravity',
                            project=self.root_name, draft_present=False, user_messages=0)

    async def submit(self, cdp, message):
        if self.current['draft_present'] or self.current['busy']:
            raise GUIError('Existing draft or busy')
        if self.submit_error: raise self.submit_error
        self.sent.append(message)
        self.current.update(url='https://127.0.0.1:1234/c/owned-task', user_messages=1)
        return {'url': self.current['url'], 'acknowledged': True}

    async def inspect_task(self, cdp, record):
        if getattr(self, 'use_real_inspect', False):
            return await super().inspect_task(cdp, record)
        return dict(self.inspection)


def fake(root):
    g = FakeGUI(root); g.root_name = root.name
    return g


@pytest.mark.asyncio
async def test_explicit_gui_container_keeps_the_authorized_file_scope_separate(tmp_path):
    g = fake(tmp_path)
    target = tmp_path / 'different-workspace'; target.mkdir()
    with pytest.raises(GUIError):
        await g.start('Work', str(target))
    g.root_name = tmp_path.name
    task = await g.start('Work', str(target), gui_project=tmp_path.name)
    assert task['workspace'] == str(target) and task['gui_project'] == tmp_path.name
    assert task['workspace_binding'] == 'explicit_path'
    assert f'Work only inside {target}.' in g.sent[0]
    assert g.load(str(target), task['task_id'])['workspace'] == str(target)


@pytest.mark.asyncio
async def test_start_refuses_when_draft_present_before_new_conversation(tmp_path):
    g = fake(tmp_path); g.current['draft_present'] = True
    with pytest.raises(GUIError, match='unsent draft'):
        await g.start('Please inspect this project.', str(tmp_path))
    assert g.new_count == 0 and len(g.sent) == 0


@pytest.mark.asyncio
async def test_new_conversation_starts_fresh_and_persists_acknowledged_url(tmp_path):
    g = fake(tmp_path); g.current['draft_present'] = False; g.current['user_messages'] = 1
    result = await g.start('Please inspect this project.', str(tmp_path))
    assert g.new_count == 1 and len(g.sent) == 1
    assert result['state'] == 'submitted' and '/c/owned-task' in result['url']
    assert result['marker'] in g.sent[0] and str(tmp_path) in g.sent[0]
    assert result['model'] == 'Gemini 3.8 Flash High'
    path = g.record_path(tmp_path, result['task_id'])
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text())['message_sha256']


@pytest.mark.asyncio
@pytest.mark.parametrize('changes', [{'busy': True}, {'model': 'Different model'}, {'project': 'wrong-project'}])
async def test_busy_wrong_model_or_wrong_project_never_receives_a_prompt(tmp_path, changes):
    g = fake(tmp_path); g.current.update(changes)
    with pytest.raises(GUIError): await g.start('Work', str(tmp_path))
    assert not g.sent and not g.new_count


@pytest.mark.asyncio
async def test_reusing_a_conversation_never_overwrites_an_unsent_draft(tmp_path):
    g = fake(tmp_path); g.current['draft_present'] = True
    with pytest.raises(GUIError): await g.start('Work', str(tmp_path), new_conversation=False)
    assert not g.sent and not g.new_count


@pytest.mark.asyncio
async def test_failed_submission_is_unconfirmed_not_falsely_successful(tmp_path):
    g = fake(tmp_path); g.submit_error = GUIError('Acknowledgement unavailable')
    with pytest.raises(GUIError) as error:
        await g.start('Work', str(tmp_path))
    record = g.load(str(tmp_path), error.value.task_id)
    assert record['state'] == 'unconfirmed'


@pytest.mark.asyncio
async def test_correction_stays_in_the_same_task_and_requires_a_completed_gui_turn(tmp_path):
    g = fake(tmp_path); task = await g.start('Work', str(tmp_path))
    g.inspection = {'state': 'running'}
    with pytest.raises(GUIError): await g.followup(str(tmp_path), task['task_id'], 'Fix the tests')
    assert len(g.sent) == 1
    g.inspection = {'state': 'ready_for_review', 'response': 'Done'}
    result = await g.followup(str(tmp_path), task['task_id'], 'Fix the tests')
    assert result['round'] == 2 and result['task_id'] == task['task_id']
    assert len(g.sent) == 2 and 'round:2' in g.sent[1] and 'Fix the tests' in g.sent[1]
    assert result['state'] == 'submitted' and 'response' not in result


@pytest.mark.parametrize('task_id', ['../escape', '/tmp/file', 'invalid', 'a' * 33])
def test_task_ids_cannot_escape_the_workspace(tmp_path, task_id):
    with pytest.raises(GUIError): AntigravityGUI.record_path(tmp_path, task_id)


def test_symlinked_state_directory_cannot_write_outside_the_workspace(tmp_path):
    project = tmp_path / 'project'; project.mkdir()
    outside = tmp_path / 'outside'; outside.mkdir()
    (project / '.codex-gravity').symlink_to(outside, target_is_directory=True)
    with pytest.raises(GUIError): AntigravityGUI.record_path(project, 'a' * 32)


class EvalCDP:
    def __init__(self, observations):
        self.observations = observations
        self.last_expression = None

    async def evaluate(self, expression):
        self.last_expression = expression
        return dict(self.observations)


@pytest.mark.asyncio
@pytest.mark.parametrize('observed,expected', [
    ({'acknowledged': False}, 'unconfirmed'),
    ({'acknowledged': True, 'intervening_user_message': True}, 'needs_attention'),
    ({'acknowledged': True, 'needs_approval': True, 'done': True, 'response': 'Done'}, 'needs_attention'),
    ({'acknowledged': True, 'busy': True, 'done': True, 'response': 'Partial'}, 'running'),
    ({'acknowledged': True, 'busy': False, 'done': True, 'response': 'Finished'}, 'ready_for_review'),
])
async def test_gui_progress_does_not_mistake_partial_or_unrelated_text_for_completion(tmp_path, observed, expected):
    g = fake(tmp_path)
    record = {'url': g.current['url'], 'marker': 'unique marker'}
    result = await AntigravityGUI.inspect_task(g, EvalCDP(observed), record)
    assert result['state'] == expected


@pytest.mark.asyncio
async def test_switching_conversations_never_reads_or_controls_another_chat(tmp_path):
    g = fake(tmp_path)
    record = {'url': 'https://127.0.0.1:1234/c/some-other-task', 'marker': 'marker'}
    result = await AntigravityGUI.inspect_task(g, EvalCDP({}), record)
    assert result['state'] == 'needs_attention' and 'response' not in result


@pytest.mark.asyncio
async def test_cdp_matches_response_ids_and_ignores_events():
    class Socket:
        def __init__(self): self.sent = []; self.messages = [
            json.dumps({'method': 'Page.event'}), json.dumps({'id': 999, 'result': {}}),
            json.dumps({'id': 1, 'result': {'result': {'value': 42}}})]
        async def send(self, message): self.sent.append(json.loads(message))
        async def recv(self): return self.messages.pop(0)
    socket = Socket()
    assert await CDP(socket).evaluate('6*7') == 42
    assert socket.sent[0]['method'] == 'Runtime.evaluate'


@pytest.mark.asyncio
async def test_mcp_exposes_real_gui_operations_without_old_pipeline_fallback(monkeypatch):
    from codex_gravity.mcp_server import create_mcp_server
    from unittest.mock import AsyncMock
    monkeypatch.setattr(AntigravityGUI, 'status', AsyncMock(side_effect=GUIError('GUI offline; no fallback')))
    server = create_mcp_server()
    names = [tool.name for tool in await server.list_tools()]
    assert all(name in names for name in ('antigravity_gui_status', 'antigravity_gui_start_task',
                                         'antigravity_gui_task_status', 'antigravity_gui_followup'))
    result = await server.call_tool('antigravity_gui_status', {})
    payload = json.loads(result.content[0].text)
    assert payload['status'] == 'error' and 'fallback' in payload['error']


@pytest.mark.parametrize('title,expected_valid', [
    ('my-project - Antigravity', True),
    ('file.py - my-project - Antigravity', True),
    ('chat 1 - my-project - Antigravity', True),
    ('dir - file.py - my-project - Antigravity', True),
    ('my-project — Antigravity', True),
    ('my-project – Antigravity', True),
    ('my-project-extra - Antigravity', False),
    ('extra-my-project - Antigravity', False),
    ('my-project - OtherApp', False),
    ('my-project', False),
])
def test_ensure_workspace_title_matching(tmp_path, title, expected_valid):
    project_dir = tmp_path / 'my-project'
    project_dir.mkdir()
    snapshot = {'title': title, 'project': None}
    if expected_valid:
        AntigravityGUI.ensure_workspace(snapshot, project_dir)
    else:
        with pytest.raises(GUIError, match='Select the requested project'):
            AntigravityGUI.ensure_workspace(snapshot, project_dir)


@pytest.mark.asyncio
async def test_inspect_task_blocker_expression_structure(tmp_path):
    g = fake(tmp_path)
    eval_cdp = EvalCDP({'acknowledged': True, 'done': True, 'busy': False, 'response': 'Done'})
    record = {'url': g.current['url'], 'marker': 'round:1 marker'}
    await AntigravityGUI.inspect_task(g, eval_cdp, record)
    expr = eval_cdp.last_expression
    assert 'after(e)' in expr
    assert '!e.disabled' in expr
    assert "e.getAttribute('aria-disabled')!=='true'" in expr


@pytest.mark.asyncio
async def test_followup_failure_retains_prior_response_and_persists_pending_submission(tmp_path):
    g = fake(tmp_path)
    task = await g.start('Initial instruction', str(tmp_path))
    record = g.load(str(tmp_path), task['task_id'])
    record.update({'state': 'ready_for_review', 'response': 'Turn 1 completed', 'done': True})
    g.save(record)
    g.inspection = {'state': 'ready_for_review', 'response': 'Turn 1 completed'}

    g.submit_error = GUIError('Network timeout during submit')
    with pytest.raises(GUIError, match='Network timeout during submit') as err:
        await g.followup(str(tmp_path), task['task_id'], 'Next round instruction')
    assert err.value.task_id == task['task_id']

    persisted = g.load(str(tmp_path), task['task_id'])
    assert persisted['round'] == 1
    assert persisted['response'] == 'Turn 1 completed'
    assert persisted['state'] == 'unconfirmed'
    assert 'pending_submission' in persisted
    pending = persisted['pending_submission']
    assert pending['round'] == 2
    assert f"task:{task['task_id']} round:2" in pending['marker']
    assert pending['message_sha256']


@pytest.mark.asyncio
async def test_polling_with_pending_submission_unconfirmed_and_reconciliation(tmp_path):
    g = fake(tmp_path)
    task = await g.start('Work', str(tmp_path))
    record = g.load(str(tmp_path), task['task_id'])
    record.update({
        'state': 'unconfirmed',
        'round': 1,
        'response': 'Turn 1 result',
        'done': True,
        'pending_submission': {
            'round': 2,
            'marker': f"[Codex-Gravity task:{task['task_id']} round:2]",
            'message_sha256': 'abc123hash',
        },
    })
    g.save(record)

    unack_cdp = EvalCDP({'acknowledged': False})
    res_unack = await AntigravityGUI.inspect_task(g, unack_cdp, record)
    assert res_unack['state'] == 'unconfirmed'
    assert record['response'] == 'Turn 1 result'
    assert record['round'] == 1
    assert 'pending_submission' in record

    ack_cdp = EvalCDP({'acknowledged': True, 'done': True, 'busy': False, 'response': 'Turn 2 answer'})
    res_ack = await AntigravityGUI.inspect_task(g, ack_cdp, record)
    assert res_ack['state'] == 'ready_for_review'
    assert res_ack['response'] == 'Turn 2 answer'
    assert record['round'] == 2
    assert 'pending_submission' not in record
    assert len(record['history']) == 1
    assert record['history'][0]['round'] == 1
    assert record['history'][0]['response'] == 'Turn 1 result'


@pytest.mark.asyncio
async def test_followup_success_appends_round_history_and_commits_new_round(tmp_path):
    g = fake(tmp_path)
    task = await g.start('Round 1 prompt', str(tmp_path))
    record = g.load(str(tmp_path), task['task_id'])
    record.update({'state': 'ready_for_review', 'response': 'Round 1 output', 'done': True})
    g.save(record)
    g.inspection = {'state': 'ready_for_review', 'response': 'Round 1 output'}

    res = await g.followup(str(tmp_path), task['task_id'], 'Round 2 prompt')
    assert res['round'] == 2
    assert res['state'] == 'submitted'
    assert 'response' not in res
    assert 'pending_submission' not in res
    assert len(res['history']) == 1
    assert res['history'][0]['round'] == 1
    assert res['history'][0]['response'] == 'Round 1 output'

    persisted = g.load(str(tmp_path), task['task_id'])
    assert persisted['round'] == 2
    assert persisted['history'][0]['response'] == 'Round 1 output'


@pytest.mark.asyncio
async def test_followup_refuses_when_draft_present(tmp_path):
    g = fake(tmp_path)
    task = await g.start('Round 1 prompt', str(tmp_path))
    record = g.load(str(tmp_path), task['task_id'])
    record.update({'state': 'ready_for_review', 'response': 'Round 1 output', 'done': True})
    g.save(record)
    g.inspection = {'state': 'ready_for_review', 'response': 'Round 1 output'}
    g.current['draft_present'] = True

    with pytest.raises(GUIError, match='unsent draft'):
        await g.followup(str(tmp_path), task['task_id'], 'Round 2 prompt')
    persisted = g.load(str(tmp_path), task['task_id'])
    assert persisted['round'] == 1
    assert 'pending_submission' not in persisted


@pytest.mark.asyncio
async def test_pending_poll_reads_updated_record_inside_lock_without_overwriting(tmp_path):
    g = fake(tmp_path)
    task = await g.start('Round 1 task', str(tmp_path))
    task_id = task['task_id']

    rec = g.load(str(tmp_path), task_id)
    rec.update({'state': 'ready_for_review', 'response': 'Round 1 reply', 'done': True})
    g.save(rec)

    # Acquire lock to block poll() from entering
    await g._lock.acquire()

    # Launch poll in background; it must wait on g._lock
    poll_future = asyncio.create_task(g.poll(str(tmp_path), task_id))
    await asyncio.sleep(0.02)

    # While lock is held, simulate another operation (e.g. followup) updating disk state to round 2
    rec = g.load(str(tmp_path), task_id)
    rec['round'] = 2
    rec['marker'] = f"[Codex-Gravity task:{task_id} round:2]"
    rec['history'] = [{'round': 1, 'response': 'Round 1 reply', 'done': True, 'state': 'ready_for_review'}]
    rec['response'] = 'Round 2 reply'
    rec['state'] = 'ready_for_review'
    g.save(rec)

    # Set mock GUI inspection to match round 2
    g.inspection = {'state': 'ready_for_review', 'response': 'Round 2 reply', 'done': True}

    # Release lock so pending poll can proceed
    g._lock.release()

    polled = await poll_future

    # Verify that poll read the updated record after acquiring lock
    assert polled['round'] == 2
    assert polled['response'] == 'Round 2 reply'
    assert len(polled.get('history', [])) == 1
    assert polled['history'][0]['round'] == 1

    # Verify disk state was not overwritten with stale round 1
    disk_record = g.load(str(tmp_path), task_id)
    assert disk_record['round'] == 2
    assert disk_record['response'] == 'Round 2 reply'
    assert len(disk_record.get('history', [])) == 1
    assert disk_record['history'][0]['round'] == 1


@pytest.mark.asyncio
async def test_pending_followup_reads_updated_record_inside_lock(tmp_path):
    g = fake(tmp_path)
    task = await g.start('Round 1 task', str(tmp_path))
    task_id = task['task_id']

    rec = g.load(str(tmp_path), task_id)
    rec.update({'state': 'ready_for_review', 'response': 'Round 1 reply', 'done': True})
    g.save(rec)
    g.inspection = {'state': 'ready_for_review', 'response': 'Round 1 reply', 'done': True}

    await g._lock.acquire()

    followup_future = asyncio.create_task(g.followup(str(tmp_path), task_id, 'Prompt for round 2'))
    await asyncio.sleep(0.02)

    rec = g.load(str(tmp_path), task_id)
    rec['response'] = 'Updated prior reply'
    g.save(rec)

    g._lock.release()

    res = await followup_future
    assert res['round'] == 2
    assert res['history'][0]['response'] == 'Updated prior reply'
