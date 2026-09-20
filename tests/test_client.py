"""Тесты клиента и хоста: HELO, команда, снимок по звуку и по байтам.

Запуск: py -m unittest discover -s tests
"""

from __future__ import annotations

import time
import unittest

from xconn_channel import config, framing
from xconn_channel.client import KEYS, Client
from xconn_channel.host import AgentHost
from xconn_channel.transport import AudioTransport, SampleLink

from test_agent import FakePty


class TestClientOverBytes(unittest.TestCase):
    def _pair(self):
        import collections

        to_agent: collections.deque = collections.deque()
        to_master: collections.deque = collections.deque()
        pty = FakePty([b"file1\r\n$ "])
        pty._pending += b"$ "
        host = AgentHost(
            lambda d: to_master.append(d),
            lambda t=0.0: to_agent.popleft() if to_agent else None,
            pty,
            pump_wait_ms=0,
            pump_idle_ms=0,
        )

        def master_receive(timeout=0.0):
            while host.poll(0):
                pass
            return to_master.popleft() if to_master else None

        client = Client(
            lambda d: to_agent.append(d),
            master_receive,
        )
        return client, host, pty

    def test_connect_and_cmd(self) -> None:
        client, host, pty = self._pair()
        helo = client.connect()
        self.assertEqual(helo.mode, config.BASE)
        self.assertIsNotNone(client.screen)
        self.assertIn("$ ", client.render())

        screen = client.cmd("ls")
        self.assertIn(b"ls", pty.written)
        self.assertIn("file1", screen.text())
        self.assertEqual(host.core.stats["cmds"], 1)

    def test_cmd_appends_newline(self) -> None:
        client, _, pty = self._pair()
        client.connect()
        client.cmd("pwd")
        self.assertIn(b"pwd\n", pty.written)

    def test_key_ctrl_c(self) -> None:
        client, _, pty = self._pair()
        client.connect()
        client.key("ctrl-c")
        self.assertIn(KEYS["ctrl-c"], pty.written)

    def test_ping_after_connect(self) -> None:
        client, _, _ = self._pair()
        client.connect()
        reply = client.ping()
        self.assertEqual(reply.type, config.PONG)


class TestClientOverAudio(unittest.TestCase):
    """Клиент и хост поверх SampleLink, как два конца кабеля."""

    def test_handshake_cmd_delta(self) -> None:
        link = SampleLink(noise=0.0)
        sink_c, source_c = link.end_a()
        sink_a, source_a = link.end_b()
        client_tr = AudioTransport(sink_c, source_c, config.PROBE)
        agent_tr = AudioTransport(sink_a, source_a, config.PROBE)
        pty = FakePty([b"ok\r\n$ "])
        pty._pending += b"$ "
        host = AgentHost(
            agent_tr.send,
            agent_tr.receive,
            pty,
            transport=agent_tr,
            pump_wait_ms=0,
            pump_idle_ms=0,
        )

        orig = client_tr.receive

        def receive(timeout: float = 0.0):
            deadline = time.monotonic() + max(float(timeout), 0.0)
            while True:
                while host.poll(0):
                    pass
                frame = orig(0)
                if frame:
                    return frame
                if timeout <= 0 or time.monotonic() >= deadline:
                    return None
                time.sleep(0.002)

        client = Client(client_tr.send, receive, transport=client_tr)
        helo = client.connect()
        self.assertEqual(helo.mode, config.BASE)
        self.assertEqual(client_tr.mode, config.BASE)
        self.assertEqual(agent_tr.mode, config.BASE)
        self.assertIn("$ ", client.render())

        first = client.cmd("ls")
        self.assertIn("ok", first.text())
        # Второй обмен без изменений PTY — дельта, экран тот же.
        pty.replies.append(b"")
        second = client.cmd("\n")
        self.assertEqual(second.cells, first.cells)


class TestHostHelo(unittest.TestCase):
    def test_non_helo_before_connect_is_nak_state(self) -> None:
        pty = FakePty([])
        sent = []
        host = AgentHost(
            sent.append,
            lambda t=0.0: None,
            pty,
            pump_wait_ms=0,
            pump_idle_ms=0,
        )
        raw = framing.build_frame(config.PING, 0)
        reply = host.handle(framing.parse_frame(raw))
        self.assertEqual(reply[0], config.NAK)
        self.assertEqual(reply[1][1], config.NAK_STATE)


if __name__ == "__main__":
    unittest.main()
