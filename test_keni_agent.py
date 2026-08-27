import asyncio
import json
import sys
import types
import unittest
from unittest import mock

# 单测不连网；避免模块的首次运行依赖安装分支触碰系统 Python。
sys.modules.setdefault('websockets', types.SimpleNamespace(
    InvalidStatus=type('InvalidStatus', (Exception,), {}),
    ConnectionClosed=type('ConnectionClosed', (Exception,), {}),
))

import keni_agent as agent


class FakeWS:
    def __init__(self):
        self.messages = []

    async def send(self, raw):
        self.messages.append(json.loads(raw))


class FakeProc:
    def __init__(self):
        self.pid = 4321
        self.returncode = None

    async def wait(self):
        self.returncode = -15
        return self.returncode


class FakeStdout:
    def __init__(self):
        self._chunks = [b'working\n', b'']

    async def read(self, _):
        return self._chunks.pop(0)


class FailingWS(FakeWS):
    async def send(self, raw):
        if self.messages:
            raise ConnectionError('closed')
        await super().send(raw)


class AgentSecurityTests(unittest.TestCase):
    def test_macos_dialog_passes_remote_text_as_argv(self):
        payload = '\\\" & do shell script "touch /tmp/pwned" & "\nnext'
        completed = mock.Mock(
            stdout='button returned:允许, gave up:false\n', returncode=0
        )
        with mock.patch.object(agent.subprocess, 'run', return_value=completed) as run:
            self.assertTrue(agent.macos_dialog('title', payload))

        argv = run.call_args.args[0]
        self.assertEqual(argv[-2:], ['title', payload])
        self.assertNotIn(payload, argv[2])
        self.assertIn('item 2 of argv', argv[2])

    def test_macos_dialog_timeout_and_failure_are_rejected(self):
        for stdout, code in (
            ('button returned:允许, gave up:true\n', 0),
            ('button returned:拒绝, gave up:false\n', 0),
            ('button returned:允许, gave up:false\n', 1),
        ):
            with self.subTest(stdout=stdout, code=code), mock.patch.object(
                agent.subprocess,
                'run',
                return_value=mock.Mock(stdout=stdout, returncode=code),
            ):
                self.assertFalse(agent.macos_dialog('title', 'message'))

    def test_unknown_shell_still_requires_confirmation(self):
        self.assertEqual(agent.classify('shell', 'custom-tool --write'), 'confirm')

    def test_platform_tokens_are_normalized_for_backend(self):
        with mock.patch.object(agent.platform, 'system', return_value='Windows'), \
             mock.patch.object(agent.platform, 'machine', return_value='AMD64'):
            self.assertEqual(agent.agent_platform(), 'windows')
            self.assertEqual(agent.agent_arch(), 'amd64')

    def test_windows_never_claims_an_unimplemented_sandbox(self):
        with mock.patch.object(agent, 'agent_platform', return_value='windows'), \
             mock.patch.dict(agent.os.environ, {'KENI_SANDBOX': 'appcontainer'}):
            self.assertEqual(agent.detect_sandbox_runtime(), 'none')

    def test_linux_prefers_nsjail_then_firejail(self):
        with mock.patch.object(agent, 'agent_platform', return_value='linux'), \
             mock.patch.dict(agent.os.environ, {}, clear=True), \
             mock.patch.object(agent.shutil, 'which', side_effect=lambda name: '/usr/bin/nsjail' if name == 'nsjail' else None):
            self.assertEqual(agent.detect_sandbox_runtime(), 'nsjail')

    def test_attestation_message_matches_backend_canonical_format(self):
        self.assertEqual(
            agent.build_attest_message('nonce', 'agent', 'conn', 'firejail', 1720000000),
            b'v1\x00nonce\x00agent\x00conn\x00firejail\x001720000000',
        )


class AgentLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        agent.ACTIVE_SESSIONS.clear()
        agent.EXEC_RESERVATIONS.clear()

    async def test_progress_heartbeat_keeps_same_exec_id(self):
        ws = FakeWS()
        agent.ACTIVE_SESSIONS['exec-1'] = agent.SessionInfo(
            'exec-1', 'shell', 'go test ./...'
        )
        with mock.patch.object(agent, 'EXEC_PROGRESS_INTERVAL', 0):
            task = asyncio.create_task(agent.progress_heartbeat(ws, 'exec-1'))
            while not ws.messages:
                await asyncio.sleep(0)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.assertEqual(ws.messages[0]['type'], 'agent_exec_progress')
        self.assertEqual(ws.messages[0]['exec_id'], 'exec-1')

    async def test_kill_session_terminates_process_group(self):
        proc = FakeProc()
        session = agent.SessionInfo('exec-2', 'shell', 'sleep 60')
        session.proc = proc
        agent.ACTIVE_SESSIONS['exec-2'] = session
        def killpg_side_effect(_, sig):
            if sig == 0:
                raise ProcessLookupError
        with mock.patch.object(agent.os, 'getpgid', return_value=4321), \
             mock.patch.object(agent.os, 'killpg', side_effect=killpg_side_effect) as killpg:
            self.assertTrue(await agent.kill_session('exec-2'))
        self.assertEqual(killpg.call_args_list[0], mock.call(4321, agent.signal.SIGTERM))

    async def test_output_transport_failure_reaps_process_tree(self):
        ws = FailingWS()
        proc = FakeProc()
        proc.stdout = FakeStdout()
        with mock.patch.object(
            agent.asyncio, 'create_subprocess_exec', return_value=proc
        ), mock.patch.object(agent.os, 'getpgid', return_value=4321), mock.patch.object(
            agent, 'reap_process_tree', new=mock.AsyncMock()
        ) as reap:
            await agent.execute(ws, 'exec-3', 'shell', 'echo hi', '/tmp')
        reap.assert_awaited_once_with(proc, 4321)

    async def test_phone_approval_wins_without_waiting_for_mac(self):
        event = asyncio.Event()
        result = {'approved': True}
        event.set()
        mac = asyncio.get_running_loop().create_future()
        self.assertTrue(
            await agent.wait_for_confirmation(mac, event, result, timeout=1)
        )

    async def test_phone_approval_cancels_stale_mac_dialog_task(self):
        started = asyncio.Event()
        cleaned = asyncio.Event()

        async def dialog():
            started.set()
            try:
                await asyncio.Future()
            finally:
                cleaned.set()

        mac = asyncio.create_task(dialog())
        await started.wait()
        event = asyncio.Event()
        result = {'approved': True}
        event.set()
        self.assertTrue(
            await agent.wait_for_confirmation(mac, event, result, timeout=1)
        )
        self.assertTrue(cleaned.is_set())

    async def test_legacy_empty_exec_id_keeps_protocol_compatibility(self):
        ws = FakeWS()
        with mock.patch.object(agent, 'execute', new=mock.AsyncMock()) as execute:
            await agent.handle_exec(
                ws,
                {
                    'exec_id': '',
                    'agent_id': 'agent-1',
                    'cmd_type': 'shell',
                    'instruction': 'pwd',
                },
                {},
                '/tmp',
            )
        execute.assert_awaited_once()
        self.assertEqual(ws.messages, [])
        self.assertEqual(agent.EXEC_RESERVATIONS, set())

    async def test_confirmation_stage_respects_concurrency_cap(self):
        ws = FakeWS()
        pending = {}
        agent.EXEC_RESERVATIONS.update({'busy-1', 'busy-2', 'busy-3'})
        with mock.patch.object(agent, 'macos_dialog') as dialog:
            await agent.handle_exec(
                ws,
                {
                    'exec_id': 'exec-4',
                    'agent_id': 'agent-1',
                    'cmd_type': 'shell',
                    'instruction': 'custom-tool --write',
                },
                pending,
                '/tmp',
            )
        dialog.assert_not_called()
        self.assertEqual(ws.messages[-1]['error'], 'too_many_inflight')
        self.assertEqual(pending, {})


if __name__ == '__main__':
    unittest.main()
