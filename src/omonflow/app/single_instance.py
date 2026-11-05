"""Single-instance guard over a local socket (QLocalServer/QLocalSocket).

The first instance listens; later launches forward their command
("activate", "toggle-widget", "settings") and exit.
"""

from __future__ import annotations

import getpass
import logging

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from omonflow import APP_ID

LOG = logging.getLogger("omonflow.instance")
COMMANDS = {"activate", "toggle-widget", "settings"}


def server_name(suffix: str = "") -> str:
    return f"{APP_ID}.{getpass.getuser()}{suffix}"


def send_to_running(command: str, name: str | None = None, timeout_ms: int = 500) -> bool:
    """Return True if another instance received the command."""
    sock = QLocalSocket()
    sock.connectToServer(name or server_name())
    if not sock.waitForConnected(timeout_ms):
        return False
    sock.write(command.encode() + b"\n")
    sock.waitForBytesWritten(timeout_ms)
    sock.disconnectFromServer()
    return True


class InstanceServer(QObject):
    command = Signal(str)

    def __init__(self, name: str | None = None, parent=None) -> None:
        super().__init__(parent)
        self.name = name or server_name()
        self.server = QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.UserAccessOption)
        if not self.server.listen(self.name):
            # Stale socket from a crashed instance (we already know none answered).
            QLocalServer.removeServer(self.name)
            if not self.server.listen(self.name):
                raise RuntimeError(f"cannot listen on {self.name}: {self.server.errorString()}")
        self.server.newConnection.connect(self._accept)
        self._sockets: list[QLocalSocket] = []

    def _accept(self) -> None:
        while self.server.hasPendingConnections():
            sock = self.server.nextPendingConnection()
            self._sockets.append(sock)
            sock.readyRead.connect(self._read)
            sock.disconnected.connect(self._drop)

    def _read(self) -> None:
        sock = self.sender()
        if not isinstance(sock, QLocalSocket):
            return
        for line in bytes(sock.readAll()).decode(errors="replace").splitlines():
            cmd = line.strip()
            if cmd in COMMANDS:
                LOG.info("received %s from another launch", cmd)
                self.command.emit(cmd)

    def _drop(self) -> None:
        sock = self.sender()
        if sock in self._sockets:
            self._sockets.remove(sock)
            sock.deleteLater()

    def close(self) -> None:
        for sock in self._sockets:
            sock.readyRead.disconnect(self._read)
            sock.disconnected.disconnect(self._drop)
            sock.abort()
        self._sockets.clear()
        self.server.close()
