"""Cooperate with the real Antigravity desktop GUI over its local DevTools port.

Only visible DOM controls are used. No agy/agentapi subprocess, provider API,
private application store, generated mock reply, or permission-preset changes.
The desktop app must already expose a loopback DevTools endpoint.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
import tempfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

import httpx
import websockets


class GUIError(RuntimeError):
    pass


def loopback_url(value: str, websocket: bool = False) -> str:
    p = urlparse(value)
    schemes = ('ws',) if websocket else ('http',)
    if p.scheme not in schemes or p.hostname not in ('127.0.0.1', 'localhost', '::1') or not p.port or p.username or p.password:
        raise GUIError('GUI connections must use an explicit loopback port; remote endpoints are refused.')
    if not websocket and (p.path not in ('', '/') or p.query or p.fragment):
        raise GUIError('Use the local DevTools base URL, not an application URL.')
    return value.rstrip('/')


def discover_endpoint() -> str:
    configured = os.getenv('ANTIGRAVITY_GUI_CDP_URL')
    if configured:
        return loopback_url(configured)
    for home in (Path.home() / '.config/Antigravity', Path.home() / 'Library/Application Support/Antigravity',
                 Path(os.getenv('APPDATA', str(Path.home()))) / 'Antigravity'):
        try:
            port = int((home / 'DevToolsActivePort').read_text().splitlines()[0])
            if 0 < port <= 65535:
                return f'http://127.0.0.1:{port}'
        except (OSError, ValueError, IndexError):
            pass
    raise GUIError('Antigravity GUI DevTools endpoint is unavailable. Open the desktop app with loopback remote debugging, or set ANTIGRAVITY_GUI_CDP_URL. No CLI fallback was used.')


class CDP:
    def __init__(self, socket):
        self.socket = socket
        self.sequence = 0

    async def call(self, method: str, params: dict | None = None):
        self.sequence += 1
        request_id = self.sequence
        await self.socket.send(json.dumps({'id': request_id, 'method': method, 'params': params or {}}))
        async def receive():
            while True:
                message = json.loads(await self.socket.recv())
                if message.get('id') == request_id:
                    if message.get('error'):
                        raise GUIError(f'Desktop DevTools rejected {method}.')
                    return message.get('result', {})
        return await asyncio.wait_for(receive(), 15)

    async def evaluate(self, expression: str):
        response = await self.call('Runtime.evaluate', {'expression': expression, 'returnByValue': True})
        if response.get('exceptionDetails') or response.get('result', {}).get('subtype') == 'error':
            raise GUIError('The Antigravity GUI layout changed or a required control is missing. Nothing was sent.')
        return response.get('result', {}).get('value')


# Scope every action to the main conversation's model selector. Auxiliary
# subagent panes may have a second composer and must never receive the prompt.
DOM = r"""
const visible=e=>!!(e&&e.getClientRects().length);
const models=[...document.querySelectorAll('[data-testid="model-selector-trigger"]')].filter(visible);
if(models.length!==1)throw Error('Main model selector is missing or ambiguous');
const model=models[0];
const view=model.closest('[data-testid="conversation-view"]') || document;
const box=model.closest('[data-testid="agent-input-box"]');
if(!box)throw Error('Main input scope is missing');
const editors=[...box.querySelectorAll('[contenteditable="true"][aria-label="Message input"]')].filter(visible);
if(editors.length!==1)throw Error('Main composer is missing or ambiguous');
const editor=editors[0];
const send=box.querySelector('[data-testid="send-button"]');
const busy=[...box.querySelectorAll('button')].some(e=>visible(e)&&/^(stop\b|cancel\b)/i.test(e.getAttribute('aria-label')||''));
"""


def expression(body: str) -> str:
    return '(()=>{' + DOM + body + '})()'


class AntigravityGUI:
    def __init__(self, endpoint: str | None = None):
        self.endpoint = loopback_url(endpoint) if endpoint else None
        self._lock = asyncio.Lock()

    async def windows(self):
        endpoint = self.endpoint or discover_endpoint()
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
                response = await client.get(endpoint + '/json/list')
                response.raise_for_status()
                targets = response.json()
            return [dict(target_id=t['id'], title=t.get('title', '')) for t in targets
                    if t.get('type') == 'page' and 'antigravity' in t.get('title', '').casefold()]
        except (OSError, httpx.HTTPError, ValueError, KeyError) as error:
            raise GUIError('Cannot discover local Antigravity GUI windows. No CLI fallback was used.') from error

    @asynccontextmanager
    async def connect(self, target_id: str | None = None):
        endpoint = self.endpoint or discover_endpoint()
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
                response = await client.get(endpoint + '/json/list')
                response.raise_for_status()
                targets = response.json()
            candidates = [t for t in targets if t.get('type') == 'page' and 'antigravity' in t.get('title', '').casefold()
                          and (target_id is None or t.get('id') == target_id)]
            if len(candidates) != 1:
                raise GUIError('Select one Antigravity window with target_id; no unique desktop window was found.')
            target = candidates[0]
            socket_url = loopback_url(target['webSocketDebuggerUrl'], websocket=True)
            if urlparse(socket_url).port != urlparse(endpoint).port:
                raise GUIError('DevTools redirected to an unexpected port.')
            async with websockets.connect(socket_url, proxy=None, open_timeout=5, max_size=8 * 1024 * 1024) as socket:
                yield CDP(socket), target
        except GUIError:
            raise
        except (OSError, httpx.HTTPError, asyncio.TimeoutError, ValueError, KeyError, websockets.exceptions.WebSocketException) as error:
            raise GUIError('Cannot connect to the local Antigravity desktop GUI. No CLI or API fallback was used.') from error

    async def snapshot(self, cdp: CDP):
        return await cdp.evaluate(expression(r"""
return {title:document.title,url:location.href,
 model:(model.getAttribute('aria-label')||model.innerText).replace(/^Select model, current:\s*/,''),
 project:document.querySelector('[data-testid="project-selector-trigger"]')?.innerText.trim()||null,
 user_messages:view.querySelectorAll('[data-testid="user-message-bubble"]').length,
 draft_present:!!editor.innerText.trim(),busy,
 send_enabled:!!send&&!send.disabled,
 new_conversation_available:[...document.querySelectorAll('button[aria-label="New Conversation"]')].filter(visible).length===1};
"""))

    async def status(self, target_id: str | None = None):
        if target_id is None:
            windows = await self.windows()
            if len(windows) != 1:
                return {'status': 'window_selection_required', 'windows': windows, 'transport': 'desktop-gui-cdp'}
        async with self._lock, self.connect(target_id) as (cdp, target):
            return {'status': 'connected', 'transport': 'desktop-gui-cdp', 'target_id': target['id'],
                    **await self.snapshot(cdp)}

    @staticmethod
    def workspace(path: str) -> Path:
        p = Path(path).expanduser()
        if not p.is_absolute() or not p.is_dir():
            raise GUIError('workspace_path must be an existing absolute project directory.')
        return p.resolve()

    @staticmethod
    def ensure_model(snapshot: dict, expected: str):
        if snapshot['model'].strip().casefold() != expected.strip().casefold():
            raise GUIError(f'Select {expected} in the Antigravity GUI first. Current model: {snapshot["model"]}. Nothing was sent.')

    async def new_conversation(self, cdp: CDP):
        return await cdp.evaluate("""(()=>{const b=[...document.querySelectorAll('button[aria-label="New Conversation"]')].filter(e=>e.getClientRects().length);if(b.length!==1)throw Error('New Conversation is ambiguous');b[0].click();return true})()""")

    async def submit(self, cdp: CDP, prompt: str):
        state = await self.snapshot(cdp)
        if state['busy'] or state['draft_present']:
            raise GUIError('The main GUI conversation is busy or has an unsent draft. Nothing was overwritten.')
        await cdp.evaluate(expression('editor.focus();return true;'))
        await cdp.call('Input.insertText', {'text': prompt})
        # Lexical updates its draft state on a real keystroke; insertText alone
        # can paint the DOM while leaving Send disabled. A trailing space is
        # ordinary UI input and leaves the submitted instruction unchanged.
        await cdp.call('Input.dispatchKeyEvent', {'type': 'keyDown', 'key': ' ', 'code': 'Space', 'text': ' ', 'windowsVirtualKeyCode': 32})
        await cdp.call('Input.dispatchKeyEvent', {'type': 'keyUp', 'key': ' ', 'code': 'Space', 'windowsVirtualKeyCode': 32})
        # Verify the DOM received the exact prompt before pressing Send.
        for _ in range(10):
            ready = await cdp.evaluate(expression('return {text:editor.innerText,enabled:!!send&&!send.disabled};'))
            if ready['enabled']:
                break
            await asyncio.sleep(.1)
        if ready['text'].strip() != prompt.strip() or not ready['enabled']:
            raise GUIError('The GUI did not accept the full prompt. It remains an unsent draft; do not retry automatically.')
        await cdp.evaluate(expression("if(!send||send.disabled)throw Error('Send is disabled');send.click();return true;"))
        marker = json.dumps(prompt.splitlines()[0])
        for _ in range(40):
            await asyncio.sleep(.15)
            try:
                ack = await cdp.evaluate(expression(
                    'return {url:location.href,acknowledged:[...view.querySelectorAll(\'[data-testid="user-message-bubble"]\')].some(e=>e.innerText.includes(' + marker + '))};'))
                if ack['acknowledged']:
                    return ack
            except GUIError:
                continue
        raise GUIError('Submission could not be confirmed in the GUI. Do not resend automatically.')

    @staticmethod
    def ensure_workspace(snapshot: dict, root: Path, gui_project: str | None = None):
        expected = gui_project or root.name
        project = snapshot.get('project')
        if project:
            valid = project.strip().casefold() == expected.casefold()
        else:
            title = snapshot.get('title', '').strip()
            parts = [p.strip() for p in re.split(r'\s+[-–—]\s+', title)]
            valid = (len(parts) >= 2
                     and parts[-1].casefold() == 'antigravity'
                     and parts[-2].casefold() == expected.casefold())
        if not valid:
            raise GUIError('Select the requested project in the Antigravity GUI first. Nothing was sent.')

    async def start(self, prompt: str, workspace_path: str, expected_model: str = 'Gemini 3.8 Flash High',
                    target_id: str | None = None, new_conversation: bool = True,
                    gui_project: str | None = None):
        root = self.workspace(workspace_path)
        if gui_project is not None and (not gui_project.strip() or len(gui_project) > 120 or any(c in gui_project for c in '\r\n\x00')):
            raise GUIError('gui_project must be the explicit visible project name.')
        if not prompt.strip() or len(prompt) > 100000:
            raise GUIError('Provide a nonempty prompt under 100000 characters.')
        async with self._lock, self.connect(target_id) as (cdp, target):
            before = await self.snapshot(cdp)
            self.ensure_model(before, expected_model)
            if before['busy']:
                raise GUIError('The main Antigravity conversation is busy; it was not interrupted.')
            if before['draft_present']:
                raise GUIError('The main GUI conversation has an unsent draft. Nothing was overwritten.')
            self.ensure_workspace(before, root, gui_project)
            if new_conversation and before.get('user_messages', 0):
                await self.new_conversation(cdp)
                for _ in range(20):
                    await asyncio.sleep(.15)
                    try:
                        after = await self.snapshot(cdp)
                    except GUIError:
                        continue
                    if after['url'] != before['url'] and not after['draft_present']:
                        break
                else:
                    raise GUIError('A fresh empty conversation could not be verified. Nothing was sent.')
            state = await self.snapshot(cdp)
            self.ensure_model(state, expected_model)
            self.ensure_workspace(state, root, gui_project)
            if state['draft_present']:
                raise GUIError('The main GUI conversation has an unsent draft. Nothing was overwritten.')
            task_id = uuid.uuid4().hex
            marker = f'[Codex-Gravity task:{task_id} round:1]'
            record = {'task_id': task_id, 'workspace': str(root), 'target_id': target['id'],
                      'url': state['url'], 'model': state['model'], 'round': 1, 'marker': marker,
                      'state': 'prepared', 'updated_at': time.time(),
                      'gui_project': gui_project or root.name,
                      'workspace_binding': 'explicit_path' if gui_project and gui_project != root.name else 'matching_project'}
            self.save(record)
            message = marker + '\nWork only inside ' + str(root) + '. Preserve existing user work.\n\n' + prompt
            record['message_sha256'] = hashlib.sha256(message.strip().encode()).hexdigest()
            self.save(record)
            try:
                ack = await self.submit(cdp, message)
            except Exception as error:
                record['state'] = 'unconfirmed'
                self.save(record)
                error.task_id = task_id
                raise
            record['url'] = ack['url']
            record['state'] = 'submitted'
            self.save(record)
            return record

    @staticmethod
    def record_path(root: Path, task_id: str):
        if not re.fullmatch(r'[a-f0-9]{32}', task_id):
            raise GUIError('Invalid GUI task id.')
        path = root / '.codex-gravity/gui-tasks' / (task_id + '.json')
        if not path.resolve().is_relative_to(root.resolve()):
            raise GUIError('GUI task state must remain inside the requested workspace.')
        return path

    def save(self, record: dict):
        path = self.record_path(Path(record['workspace']), record['task_id'])
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(json.dumps(record, ensure_ascii=False, indent=2))
        try:
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def load(self, workspace_path: str, task_id: str):
        root = self.workspace(workspace_path)
        record = json.loads(self.record_path(root, task_id).read_text())
        if record.get('workspace') != str(root) or record.get('task_id') != task_id:
            raise GUIError('Task record does not match this workspace.')
        return record

    async def inspect_task(self, cdp: CDP, record: dict):
        state = await self.snapshot(cdp)
        # Query parameters may select an auxiliary tab. The conversation path
        # must match, but a changed auxiliary selection is not another chat.
        if urlparse(state['url']).path != urlparse(record['url']).path:
            return {'state': 'needs_attention', 'reason': 'Open this task conversation in the GUI.', 'url': record['url']}
        pending = record.get('pending_submission')
        target_marker = pending['marker'] if pending else record['marker']
        marker = json.dumps(target_marker)
        observed = await cdp.evaluate(expression(r"""
const marker=MARKER;
const users=[...view.querySelectorAll('[data-testid="user-message-bubble"]')];
const own=users.find(e=>e.innerText.includes(marker));
if(!own)return {acknowledged:false};
const next=users.find(e=>e!==own&&(own.compareDocumentPosition(e)&Node.DOCUMENT_POSITION_FOLLOWING));
const after=e=>(own.compareDocumentPosition(e)&Node.DOCUMENT_POSITION_FOLLOWING)&&(!next||(e.compareDocumentPosition(next)&Node.DOCUMENT_POSITION_FOLLOWING));
const replies=[...view.querySelectorAll('[data-testid="planner-response-text"]')].filter(after);
// Rich Markdown replies do not carry planner-response-text. The observed GUI
// groups each user header, response body and completed toolbar in one turn.
const turn=own.closest('.scroll-mt-4');
const replyBody=turn?[...turn.children].filter(e=>!e.contains(own)&&
 !e.querySelector('[data-testid="cascade-system-message-toolbar"]')).map(e=>e.innerText).join('\n\n'):'';
const done=[...view.querySelectorAll('[data-testid="agent-status-done"]')].some(after)||
 [...view.querySelectorAll('[data-testid="cascade-system-message-toolbar"]')].some(e=>after(e)&&
 e.querySelector('button[aria-label="Good response"]')&&e.querySelector('button[aria-label="Bad response"]'));
const blockers=[...view.querySelectorAll('button')].filter(e=>visible(e)&&!e.disabled&&e.getAttribute('aria-disabled')!=='true'&&after(e)&&/^(approve|allow|accept|retry|resume)/i.test(e.getAttribute('aria-label')||e.innerText));
return {acknowledged:true,intervening_user_message:!!next,busy,done,
 response:(replyBody||replies.map(e=>e.innerText).join('\n\n')).slice(-40000),
 needs_approval:blockers.length>0};
""".replace('MARKER', marker)))
        if not observed.get('acknowledged'):
            if pending:
                return {'state': 'unconfirmed', 'acknowledged': False, 'reason': 'Pending submission not confirmed in GUI; prior response preserved.'}
            return {'state': 'unconfirmed', 'reason': 'The submitted task marker is not visible; do not resend automatically.'}
        if pending:
            prior = {
                'round': record['round'],
                'marker': record['marker'],
                'response': record.get('response'),
                'done': record.get('done', True),
                'state': record.get('state', 'ready_for_review'),
            }
            history = list(record.get('history', []))
            history.append(prior)
            record['history'] = history[-20:]
            record['round'] = pending['round']
            record['marker'] = pending['marker']
            record['message_sha256'] = pending['message_sha256']
            record.pop('pending_submission', None)
            for key in ('response', 'done', 'busy', 'acknowledged', 'needs_approval', 'reason', 'intervening_user_message'):
                record.pop(key, None)
        if observed.get('intervening_user_message'):
            return {'state': 'needs_attention', 'reason': 'A different message followed this task; refusing to attribute its response to this task.'}
        if observed.get('needs_approval'):
            status = 'needs_attention'
        elif observed.get('done') and observed.get('response') and not observed.get('busy'):
            status = 'ready_for_review'
        else:
            status = 'running'
        return {'state': status, **observed}

    async def poll(self, workspace_path: str, task_id: str):
        async with self._lock:
            record = self.load(workspace_path, task_id)
            async with self.connect(record['target_id']) as (cdp, _):
                result = await self.inspect_task(cdp, record)
                record.update(result, updated_at=time.time())
                self.save(record)
                return record

    async def followup(self, workspace_path: str, task_id: str, prompt: str):
        if not prompt.strip() or len(prompt) > 100000:
            raise GUIError('Provide a nonempty follow-up under 100000 characters.')
        async with self._lock:
            record = self.load(workspace_path, task_id)
            async with self.connect(record['target_id']) as (cdp, _):
                state = await self.snapshot(cdp)
                self.ensure_model(state, record['model'])
                self.ensure_workspace(state, Path(record['workspace']), record.get('gui_project'))
                result = await self.inspect_task(cdp, record)
                if result['state'] != 'ready_for_review':
                    raise GUIError('A follow-up requires this task to be ready_for_review in the current GUI conversation.')
                if state['busy'] or state['draft_present']:
                    raise GUIError('The main GUI conversation is busy or has an unsent draft. Nothing was overwritten.')
                next_round = record['round'] + 1
                next_marker = f'[Codex-Gravity task:{task_id} round:{next_round}]'
                message = next_marker + '\nWork only inside ' + record['workspace'] + '.\n\n' + prompt
                message_sha256 = hashlib.sha256(message.strip().encode()).hexdigest()
                pending = {
                    'round': next_round,
                    'marker': next_marker,
                    'message_sha256': message_sha256,
                }
                record['pending_submission'] = pending
                record['updated_at'] = time.time()
                self.save(record)
                try:
                    ack = await self.submit(cdp, message)
                except Exception as error:
                    record['state'] = 'unconfirmed'
                    record['updated_at'] = time.time()
                    self.save(record)
                    error.task_id = task_id
                    raise
                prior = {
                    'round': record['round'],
                    'marker': record['marker'],
                    'response': record.get('response') or result.get('response'),
                    'done': record.get('done', True),
                    'state': record.get('state', 'ready_for_review'),
                }
                history = list(record.get('history', []))
                history.append(prior)
                record['history'] = history[-20:]
                record['round'] = pending['round']
                record['marker'] = pending['marker']
                record['message_sha256'] = pending['message_sha256']
                record.pop('pending_submission', None)
                for key in ('response', 'done', 'busy', 'acknowledged', 'needs_approval', 'reason', 'intervening_user_message'):
                    record.pop(key, None)
                record['url'] = ack['url']
                record.update(state='submitted', updated_at=time.time())
                self.save(record)
                return record
