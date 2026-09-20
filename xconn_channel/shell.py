"""Оболочка агента: PTY на Linux, трубы на Windows.

Ядро агента (AgentCore) не открывает PTY само — оно получает write/read.
Здесь обвязка ОС: живой bash в настоящем терминале на сервере и запасной
вариант через трубы, чтобы loopback крутился на ноутбуке без pty.

На Linux предпочтителен PtyShell: программы видят TIOCGWINSZ, SIGWINCH и
isatty. PipeShell — не TTY, vim/htop ведут себя как к файлу; для `ip a` и
правки конфигов этого достаточно, для полноценного экрана — нет.

Сетевых импортов нет (AGENTS.md 3.2).
"""

from __future__ import annotations

import os
import queue
import signal
import subprocess
import threading


class PipeShell:
    """Постоянный процесс с stdin/stdout. Не TTY, работает на Windows."""

    def __init__(self, argv=None, rows: int = 24, cols: int = 80) -> None:
        if argv is None:
            argv = ["cmd.exe"] if os.name == "nt" else ["/bin/bash", "-i"]
        self._argv = list(argv)
        self.rows = rows
        self.cols = cols
        self._q: queue.Queue[bytes] = queue.Queue()
        self.proc = self._spawn()
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    def _spawn(self) -> subprocess.Popen:
        env = os.environ.copy()
        env["TERM"] = "vt100"
        env["LINES"] = str(self.rows)
        env["COLUMNS"] = str(self.cols)
        kwargs: dict = dict(
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            env=env,
        )
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        return subprocess.Popen(self._argv, **kwargs)

    def _reader(self) -> None:
        assert self.proc.stdout is not None
        while True:
            data = self.proc.stdout.read(1024)
            if not data:
                break
            self._q.put(data)

    def write(self, data: bytes) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def read(self) -> bytes:
        chunks = []
        while True:
            try:
                chunks.append(self._q.get_nowait())
            except queue.Empty:
                break
        return b"".join(chunks)

    def resize(self, rows: int, cols: int) -> None:
        self.rows, self.cols = rows, cols

    def child_exited(self) -> bool:
        return self.proc.poll() is not None

    def restart(self) -> None:
        self.close()
        self._q = queue.Queue()
        self.proc = self._spawn()
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    def close(self) -> None:
        if self.proc.stdin is not None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=1)
        if self.proc.stdout is not None:
            try:
                self.proc.stdout.close()
            except OSError:
                pass


class PtyShell:
    """Настоящий PTY + дочерний процесс. Только Unix."""

    def __init__(self, argv=None, rows: int = 24, cols: int = 80) -> None:
        self._argv = list(argv) if argv is not None else ["/bin/bash", "-l"]
        self.rows = rows
        self.cols = cols
        self.pid = 0
        self.fd = -1
        self._spawn()

    def _spawn(self) -> None:
        import fcntl
        import pty

        pid, master_fd = pty.fork()
        if pid == 0:
            os.environ["TERM"] = "vt100"
            os.environ["LINES"] = str(self.rows)
            os.environ["COLUMNS"] = str(self.cols)
            os.execvp(self._argv[0], self._argv)
        self.pid = pid
        self.fd = master_fd
        flags = fcntl.fcntl(self.fd, fcntl.F_GETFL)
        fcntl.fcntl(self.fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        self.resize(self.rows, self.cols)

    def write(self, data: bytes) -> None:
        os.write(self.fd, data)

    def read(self) -> bytes:
        try:
            return os.read(self.fd, 4096)
        except BlockingIOError:
            return b""
        except OSError:
            return b""

    def resize(self, rows: int, cols: int) -> None:
        import fcntl
        import struct
        import termios

        self.rows, self.cols = rows, cols
        packed = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, packed)
        try:
            os.kill(self.pid, signal.SIGWINCH)
        except OSError:
            pass

    def child_exited(self) -> bool:
        pid, _status = os.waitpid(self.pid, os.WNOHANG)
        return pid != 0

    def restart(self) -> None:
        if self.fd >= 0:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = -1
        try:
            os.kill(self.pid, signal.SIGKILL)
            os.waitpid(self.pid, 0)
        except OSError:
            pass
        self._spawn()

    def close(self) -> None:
        if self.fd >= 0:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = -1
        if self.pid:
            try:
                os.kill(self.pid, signal.SIGHUP)
                os.waitpid(self.pid, 0)
            except OSError:
                pass
            self.pid = 0


def open_shell(argv=None, rows: int = 24, cols: int = 80):
    """PTY на Unix, трубы иначе. Агент на сервере обязан попасть в PtyShell."""
    if os.name != "nt":
        try:
            return PtyShell(argv, rows, cols)
        except (OSError, ImportError):
            pass
    return PipeShell(argv, rows, cols)
