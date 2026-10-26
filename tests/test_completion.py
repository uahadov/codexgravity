import json
import sys
import time
from unittest.mock import AsyncMock

import pytest

from codex_gravity.codex_bridge import BridgeError, DesktopCodex, run_codex_architect, thread_id
from codex_gravity.completion import CompletionWatches

THREAD = '01a168f9-d943-7a50-be88-2b7a7579a3c9'
TASK = 'a' * 32


@pytest.fixture
def bridge(tmp_path):
    class GUI:
        def __init__(self):
            self.task = {'task_id': TASK, 'workspace': str(tmp_path), 'round': 1, 'state': 'submitted'}
        def load(self, root, task_id):
            assert root == str(tmp_path) and task_id == TASK
            return dict(self.task)
        async def poll(self, root, task_id):
            return self.load(root, task_id)
    class Desktop:
        caller_thread = THREAD
        def __init__(self):
            self.state = 'idle'
            self.sent = []
            self.fail = False
        async def read_thread(self, destination):
            assert destination == THREAD
            return {'id': THREAD, 'kind': 'codex', 'cwd': str(tmp_path), 'status': {'type': self.state}}
        async def send(self, destination, text):
            self.sent.append((destination, text))
            if self.fail:
                raise TimeoutError('acknowledgement lost')
            return {'status': 'queued', 'threadId': destination}
    return CompletionWatches(gui=GUI(), desktop=Desktop())


async def register(bridge, tmp_path):
    return await bridge.register(str(tmp_path), TASK, THREAD)


@pytest.mark.asyncio
async def test_only_complete_owned_response_notifies_exact_chat_once(bridge, tmp_path):
    record = await register(bridge, tmp_path)
    assert (await bridge.step(str(tmp_path), record['watch_id']))['state'] == 'watching'
    assert not bridge.desktop.sent
    bridge.gui.task.update(state='ready_for_review', response='Actual files changed; inspect them.', busy=False)
    result = await bridge.step(str(tmp_path), record['watch_id'])
    assert result['state'] == 'notified'
    assert bridge.desktop.sent[0][0] == THREAD
    assert 'not verified success' in bridge.desktop.sent[0][1]
    assert TASK in bridge.desktop.sent[0][1] and str(tmp_path) in bridge.desktop.sent[0][1]
    await bridge.step(str(tmp_path), record['watch_id'])
    again = await register(bridge, tmp_path)
    assert again['state'] == 'notified' and len(bridge.desktop.sent) == 1


@pytest.mark.asyncio
async def test_busy_codex_is_not_interrupted(bridge, tmp_path):
    record = await register(bridge, tmp_path)
    bridge.gui.task.update(state='ready_for_review', response='done')
    bridge.desktop.state = 'active'
    assert (await bridge.step(str(tmp_path), record['watch_id']))['state'] == 'awaiting_codex_idle'
    assert not bridge.desktop.sent
    bridge.desktop.state = 'idle'
    assert (await bridge.step(str(tmp_path), record['watch_id']))['state'] == 'notified'


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [dict(state='ready_for_review', response=''),
                                    dict(state='ready_for_review', response='partial', busy=True),
                                    dict(state='unconfirmed'), dict(round=2),
                                    dict(state='needs_attention', reason='Wrong GUI chat'),
                                    dict(state='ready_for_review', response='done', pending_submission={'round': 2})])
async def test_ambiguous_or_changed_task_never_notifies(bridge, tmp_path, change):
    record = await register(bridge, tmp_path)
    bridge.gui.task.update(change)
    assert (await bridge.step(str(tmp_path), record['watch_id']))['state'] == 'needs_attention'
    assert not bridge.desktop.sent


@pytest.mark.asyncio
async def test_uncertain_delivery_is_not_resent_after_restart_or_resume(bridge, tmp_path):
    record = await register(bridge, tmp_path)
    bridge.gui.task.update(state='ready_for_review', response='done')
    bridge.desktop.fail = True
    assert (await bridge.step(str(tmp_path), record['watch_id']))['state'] == 'delivery_unconfirmed'
    restarted = CompletionWatches(gui=bridge.gui, desktop=bridge.desktop)
    await restarted.step(str(tmp_path), record['watch_id'])
    result = await restarted.register(str(tmp_path), TASK, THREAD, resume=True)
    assert result['state'] == 'delivery_unconfirmed' and len(bridge.desktop.sent) == 1


@pytest.mark.asyncio
async def test_crash_after_delivery_started_requires_manual_reconciliation(bridge, tmp_path):
    record = await register(bridge, tmp_path)
    record['state'] = 'delivery_in_progress'
    bridge.save(record)
    result = await bridge.step(str(tmp_path), record['watch_id'])
    assert result['state'] == 'delivery_unconfirmed' and not bridge.desktop.sent


@pytest.mark.asyncio
async def test_cancel_timeout_and_explicit_resume(bridge, tmp_path):
    record = await register(bridge, tmp_path)
    bridge.cancel(str(tmp_path), record['watch_id'])
    assert (await bridge.step(str(tmp_path), record['watch_id']))['state'] == 'cancelled'
    record = await bridge.register(str(tmp_path), TASK, THREAD, resume=True)
    assert record['state'] == 'watching'
    record['deadline'] = time.time() - 1
    bridge.save(record)
    assert (await bridge.step(str(tmp_path), record['watch_id']))['state'] == 'timed_out'
    assert not bridge.desktop.sent


@pytest.mark.asyncio
async def test_new_round_has_independent_notification(bridge, tmp_path):
    one = await register(bridge, tmp_path)
    bridge.gui.task.update(state='ready_for_review', response='round one')
    await bridge.step(str(tmp_path), one['watch_id'])
    bridge.gui.task.update(round=2, state='submitted')
    two = await register(bridge, tmp_path)
    assert one['watch_id'] != two['watch_id']
    bridge.gui.task.update(state='ready_for_review', response='round two')
    await bridge.step(str(tmp_path), two['watch_id'])
    assert len(bridge.desktop.sent) == 2


@pytest.mark.asyncio
async def test_private_state_and_single_worker_lock(bridge, tmp_path, monkeypatch):
    record = await register(bridge, tmp_path)
    path = bridge.path(str(tmp_path), record['watch_id'])
    assert path.stat().st_mode & 0o777 == 0o600
    spawn = AsyncMock()
    monkeypatch.setattr('codex_gravity.completion.subprocess.Popen', spawn)
    with bridge.lock(str(tmp_path), record['watch_id']) as first:
        assert first
        bridge.spawn(record)
    spawn.assert_not_called()


@pytest.mark.parametrize('directory', ['.codex-gravity', '.codex-gravity/completion-watches'])
def test_state_directory_symlinks_cannot_escape_workspace(tmp_path, directory):
    outside = tmp_path.parent / (tmp_path.name + '-outside')
    outside.mkdir()
    target = tmp_path / directory
    target.parent.mkdir(exist_ok=True)
    target.symlink_to(outside, target_is_directory=True)
    with pytest.raises(BridgeError):
        CompletionWatches.path(str(tmp_path), 'b' * 32)
    assert not list(outside.iterdir())


@pytest.mark.parametrize('value', ['--last', 'some chat', '../x', '', THREAD.upper()])
def test_exact_thread_uuid_required(value):
    with pytest.raises(BridgeError):
        thread_id(value)


@pytest.mark.asyncio
async def test_desktop_adapter_rejects_wrong_destination_and_unknown_ack(monkeypatch):
    adapter = object.__new__(DesktopCodex)
    adapter._call = AsyncMock(return_value={'thread': {'id': 'other', 'kind': 'codex'}})
    with pytest.raises(BridgeError):
        await adapter.read_thread(THREAD)
    adapter._call = AsyncMock(return_value={'status': 'completed'})
    with pytest.raises(BridgeError):
        await adapter.send(THREAD, 'done')


def executable(tmp_path, code):
    path = tmp_path / 'fake-codex'
    path.write_text('#!' + sys.executable + '\n' + code)
    path.chmod(0o700)
    return str(path)


@pytest.mark.asyncio
async def test_codex_cli_receives_context_via_stdin_and_returns_actual_file(tmp_path, monkeypatch):
    marker = tmp_path / 'must-not-be-created'
    monkeypatch.setenv('OPENAI_API_KEY', 'unused-test-key')
    monkeypatch.setenv('CODEX_API_KEY', 'unused-test-key')
    binary = executable(tmp_path, '''import sys,json,pathlib,os
assert 'OPENAI_API_KEY' not in os.environ and 'CODEX_API_KEY' not in os.environ
args=sys.argv[1:]
assert 'exec' in args and '--ephemeral' in args
assert args[args.index('--sandbox')+1]=='read-only'
assert 'mcp_servers={}' in args
text=sys.stdin.read()
assert 'test-context' in text
assert 'test-context' not in ' '.join(args)
pathlib.Path(args[args.index('--output-last-message')+1]).write_text('Real subprocess output')
print(json.dumps({'type':'turn.completed'}))
''')
    monkeypatch.setenv('CODEX_GRAVITY_CODEX_BIN', binary)
    result = await run_codex_architect(f'$(touch {marker})', 'test-context', str(tmp_path))
    assert result['answer'] == 'Real subprocess output' and result['provider'] == 'codex'
    assert not marker.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('body', ['raise SystemExit(2)', 'print(\'{"type":"turn.failed"}\')'])
async def test_codex_failure_never_returns_mock_success(tmp_path, monkeypatch, body):
    monkeypatch.setenv('CODEX_GRAVITY_CODEX_BIN', executable(tmp_path, body))
    with pytest.raises(BridgeError):
        await run_codex_architect('request', workspace=str(tmp_path))


@pytest.mark.asyncio
async def test_codex_timeout_stops_child(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEX_GRAVITY_CODEX_BIN', executable(tmp_path, 'import time; time.sleep(30)'))
    with pytest.raises(TimeoutError):
        await run_codex_architect('request', workspace=str(tmp_path), timeout=1)


@pytest.mark.asyncio
async def test_native_minimal_ack_is_accepted_but_mismatched_id_is_not():
    adapter = object.__new__(DesktopCodex)
    adapter._call = AsyncMock(return_value={'threadId': THREAD})
    assert (await adapter.send(THREAD, 'done'))['threadId'] == THREAD
    adapter._call = AsyncMock(return_value={'status': 'queued', 'threadId': 'other'})
    with pytest.raises(BridgeError):
        await adapter.send(THREAD, 'done')
    adapter._call = AsyncMock(return_value={'threadId': THREAD, 'error': 'rejected'})
    with pytest.raises(BridgeError):
        await adapter.send(THREAD, 'done')


@pytest.mark.asyncio
async def test_finished_gui_need_not_stay_visible_while_codex_is_busy(bridge, tmp_path):
    record = await register(bridge, tmp_path)
    bridge.gui.task.update(state='ready_for_review', response='done')
    bridge.desktop.state = 'active'
    await bridge.step(str(tmp_path), record['watch_id'])
    bridge.gui.poll = AsyncMock(side_effect=AssertionError('Do not poll the GUI after completion was captured'))
    bridge.desktop.state = 'idle'
    assert (await bridge.step(str(tmp_path), record['watch_id']))['state'] == 'notified'


@pytest.mark.asyncio
async def test_mcp_codex_errors_and_completion_tool_schema(monkeypatch):
    from codex_gravity.mcp_server import create_mcp_server
    monkeypatch.setattr('codex_gravity.mcp_server.run_codex_architect', AsyncMock(side_effect=BridgeError('CLI offline')))
    server = create_mcp_server()
    response = await server.call_tool('call_codex_architect', {'prompt': 'request'})
    result = json.loads(response.content[0].text)
    assert result['status'] == 'error' and 'answer' not in result
    names = {t.name for t in await server.list_tools()}
    assert {'codex_gravity_watch_completion', 'codex_gravity_completion_status',
            'codex_gravity_cancel_completion'} <= names
