import asyncio
import contextlib
import io
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
    async def read(self, _):
        return b'working\n'


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
            agent.asyncio, 'create_subprocess_shell', return_value=proc
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


class RedeemErrorMessageTests(unittest.TestCase):
    """429 两档的补救动作互不相同，措辞不能混用。"""

    @staticmethod
    def _err(code, body, headers=None):
        return types.SimpleNamespace(
            code=code,
            headers=headers or {},
            read=lambda: json.dumps(body).encode(),
        )

    def _capture(self, e):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            agent._print_redeem_error(e, 'ws://example.test/api/v1/agent/ws')
        return buf.getvalue()

    def test_failed_attempts_branch_never_tells_user_to_wait(self):
        # 后端在这一档不下发秒数；即便下发也不该照抄：该计数器只在“码查无”时累计，
        # 等待不会让这个码变回有效，正确动作是回 APP 换新码。
        for headers, body in (
            ({}, {'error': 'too_many_failed_attempts'}),
            ({'Retry-After': '3600'},
             {'error': 'too_many_failed_attempts', 'retry_after': 3600}),
        ):
            with self.subTest(headers=headers):
                out = self._capture(self._err(429, body, headers))
                self.assertNotIn('重试', out)
                self.assertNotIn('稍后', out)
                self.assertNotIn('3600', out)
                self.assertIn('换一个新码', out)

    def test_entry_rate_limit_branch_still_consumes_retry_after(self):
        # A 闸拦在校验之前，码可能仍有效 ⇒ 这一档的等待建议是对的，秒数要照常读。
        out = self._capture(
            self._err(429, {'error': 'rate_limited', 'retry_after': 42}))
        self.assertIn('42 秒后重试', out)
        self.assertIn('不要重新生成', out)

    def test_entry_rate_limit_without_seconds_falls_back(self):
        out = self._capture(self._err(429, {'error': 'rate_limited'}))
        self.assertIn('请稍后重试', out)


if __name__ == '__main__':
    unittest.main()
