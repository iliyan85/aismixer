"""Async Unix-domain socket transport for routing control.

This module owns only local Unix-domain listener lifecycle and newline-delimited
JSON framing. Complete JSON frames are delegated to RoutingControlProtocol, which
in turn delegates routing authority to RoutingControlService and RoutingState.

Filesystem ownership and permissions on the socket path are the authorization
boundary for this transport. Runtime wiring is intentionally deferred so the
service startup path remains unchanged for now.
"""

from __future__ import annotations

import asyncio
import enum
import inspect
import logging
import os
import socket
import stat
from typing import Final

from core.routing_control_protocol import (
    RoutingControlProtocol,
    build_error_response,
    encode_json_response,
)


logger = logging.getLogger(__name__)

ERROR_FRAME_TOO_LARGE: Final = "frame_too_large"
ERROR_INTERNAL_ERROR: Final = "internal_error"
FRAME_TOO_LARGE_MESSAGE: Final = "Routing control request exceeds maximum frame size."
INTERNAL_ERROR_MESSAGE: Final = "Internal routing control error."
_READ_CHUNK_SIZE: Final = 65_536
_STALE_SOCKET_PROBE_TIMEOUT_SECONDS: Final = 1.0

_SocketIdentity = tuple[int, int]


class _SocketLiveness(enum.Enum):
    """Classification of an existing Unix socket path before it is replaced."""

    LIVE = "live"
    STALE = "stale"
    UNCERTAIN = "uncertain"


class ControlSocketPathError(OSError):
    """Raised when the configured Unix control socket path is unsafe."""

    def __init__(self, path: str, message: str):
        self.path = path
        super().__init__(f"{message}: {path!r}")


class RoutingControlUnixServer:
    """Async Unix-domain NDJSON server for RoutingControlProtocol.

    The server accepts concurrent local clients over a filesystem socket. Each
    connection may send multiple newline-delimited JSON requests, which are
    processed sequentially on that connection and delegated unchanged to the
    protocol layer without the trailing newline. Responses are compact JSON
    protocol responses followed by exactly one newline byte.

    The socket mode defaults to ``0o660``; at this stage filesystem ownership
    and group membership are the authorization boundary. The transport does not
    inspect routing state, compile routing configs, or implement routing methods.
    """

    def __init__(
        self,
        protocol: RoutingControlProtocol,
        socket_path: str | os.PathLike[str],
        *,
        max_request_bytes: int = 1_048_576,
        socket_mode: int = 0o660,
    ):
        if not isinstance(protocol, RoutingControlProtocol):
            raise TypeError("protocol must be a RoutingControlProtocol.")

        try:
            path = os.fspath(socket_path)
        except TypeError as exc:
            raise TypeError("socket_path must be a filesystem path.") from exc
        if not isinstance(path, str):
            raise TypeError("socket_path must be a string filesystem path.")
        if not path:
            raise ValueError("socket_path must be non-empty.")

        if isinstance(max_request_bytes, bool) or not isinstance(max_request_bytes, int):
            raise TypeError("max_request_bytes must be a positive integer.")
        if max_request_bytes <= 0:
            raise ValueError("max_request_bytes must be a positive integer.")

        if isinstance(socket_mode, bool) or not isinstance(socket_mode, int):
            raise TypeError("socket_mode must be an integer permission mode.")
        if socket_mode < 0 or socket_mode > 0o777:
            raise ValueError("socket_mode must be between 0o000 and 0o777.")

        self._protocol = protocol
        self._socket_path = path
        self._max_request_bytes = max_request_bytes
        self._socket_mode = socket_mode
        self._server: asyncio.AbstractServer | None = None
        self._socket_identity: _SocketIdentity | None = None

    @property
    def is_running(self) -> bool:
        """Return whether the Unix listener is currently started."""

        return self._server is not None

    async def start(self) -> None:
        """Create the Unix-domain listener without entering serve_forever()."""

        if self._server is not None:
            raise RuntimeError("Routing control Unix server is already running.")

        start_unix_server = getattr(asyncio, "start_unix_server", None)
        if start_unix_server is None:
            raise RuntimeError(
                "asyncio Unix-domain servers are not supported on this platform."
            )

        self._prepare_socket_path()

        # Own the raw socket until asyncio takes it over. It is bound but not
        # listening, its permissions are set to the exact configured mode, and
        # only then is it handed to the listener.
        raw_socket = _bind_control_socket(self._socket_path, self._socket_mode)
        server = None
        socket_identity = None
        try:
            socket_identity = _socket_identity(self._socket_path)
            if socket_identity is None:
                raise ControlSocketPathError(
                    self._socket_path,
                    "Created control socket path is not a Unix socket",
                )
            os.chmod(self._socket_path, self._socket_mode)
            server = await start_unix_server(
                self._handle_connection,
                sock=raw_socket,
            )
        except BaseException:
            if server is not None:
                server.close()
                await server.wait_closed()
            else:
                raw_socket.close()
            if socket_identity is not None:
                _remove_matching_socket(
                    self._socket_path,
                    socket_identity,
                    log_errors=True,
                )
            raise

        self._server = server
        self._socket_identity = socket_identity

    async def close(self) -> None:
        """Stop the listener and remove the socket created by this instance."""

        server = self._server
        socket_identity = self._socket_identity
        self._server = None
        self._socket_identity = None

        if server is not None:
            server.close()
            await server.wait_closed()

        if socket_identity is not None:
            _remove_matching_socket(
                self._socket_path,
                socket_identity,
                log_errors=True,
            )

    async def serve_forever(self) -> None:
        """Serve until cancelled.

        The listener must already be started with start(); this keeps
        construction deterministic and avoids binding sockets from __init__.
        """

        if self._server is None:
            raise RuntimeError("Routing control Unix server is not running.")
        await self._server.serve_forever()

    async def _handle_connection(self, reader, writer) -> None:
        try:
            await self._serve_connection(reader, writer)
        except asyncio.CancelledError:
            raise
        except (BrokenPipeError, ConnectionError):
            pass
        except Exception:
            logger.exception("Routing control Unix connection handler failed.")
        finally:
            await _close_writer(writer)

    async def _serve_connection(self, reader, writer) -> None:
        buffer = b""

        while True:
            newline_at = buffer.find(b"\n")
            if newline_at >= 0:
                frame = buffer[:newline_at]
                buffer = buffer[newline_at + 1 :]
                if len(frame) > self._max_request_bytes:
                    await self._write_transport_error(
                        writer,
                        ERROR_FRAME_TOO_LARGE,
                        FRAME_TOO_LARGE_MESSAGE,
                    )
                    return
                if not await self._process_frame(frame, writer):
                    return
                continue

            if len(buffer) > self._max_request_bytes:
                await self._write_transport_error(
                    writer,
                    ERROR_FRAME_TOO_LARGE,
                    FRAME_TOO_LARGE_MESSAGE,
                )
                return

            read_size = min(
                _READ_CHUNK_SIZE,
                self._max_request_bytes + 1 - len(buffer),
            )
            if read_size <= 0:
                read_size = 1

            chunk = await reader.read(read_size)
            if not chunk:
                # Only complete newline-delimited frames are executable. Any
                # trailing partial frame at EOF is discarded without dispatch
                # and without a response.
                if buffer:
                    logger.debug(
                        "Discarding %d-byte partial routing control frame at EOF.",
                        len(buffer),
                    )
                return
            buffer += chunk

    async def _process_frame(self, frame: bytes, writer) -> bool:
        try:
            response = self._protocol.handle_json(frame)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Unexpected routing control protocol error.")
            await self._write_transport_error(
                writer,
                ERROR_INTERNAL_ERROR,
                INTERNAL_ERROR_MESSAGE,
            )
            return False

        return await _write_response(writer, response)

    async def _write_transport_error(
        self,
        writer,
        code: str,
        message: str,
    ) -> bool:
        response = encode_json_response(
            build_error_response(
                request_id=None,
                code=code,
                message=message,
            )
        )
        return await _write_response(writer, response)

    def _prepare_socket_path(self) -> None:
        parent = os.path.dirname(os.path.abspath(self._socket_path))
        if not os.path.isdir(parent):
            raise ControlSocketPathError(
                self._socket_path,
                "Control socket parent directory does not exist",
            )

        try:
            path_stat = os.lstat(self._socket_path)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise ControlSocketPathError(
                self._socket_path,
                "Control socket path cannot be inspected",
            ) from exc

        if not stat.S_ISSOCK(path_stat.st_mode):
            raise ControlSocketPathError(
                self._socket_path,
                "Refusing to replace "
                f"{_path_type(path_stat.st_mode)} at control socket path",
            )

        liveness = _probe_unix_socket(self._socket_path)
        if liveness is _SocketLiveness.LIVE:
            raise ControlSocketPathError(
                self._socket_path,
                "Refusing to replace a live control socket",
            )
        if liveness is not _SocketLiveness.STALE:
            raise ControlSocketPathError(
                self._socket_path,
                "Existing control socket could not be confirmed stale",
            )

        self._remove_stale_control_socket(
            (path_stat.st_dev, path_stat.st_ino),
        )

    def _remove_stale_control_socket(self, identity: _SocketIdentity) -> None:
        """Unlink the path only if it is still the exact stale socket observed.

        Guards the window between the liveness probe and ``unlink`` against a
        concurrent process replacing the node: a vanished path is fine, but a
        different inode, a non-socket, or a now-live endpoint all fail closed
        rather than deleting a replacement. The identity is re-checked once more
        immediately after the liveness re-probe, since that probe briefly
        reconnects and the node may be swapped again before ``unlink``. This is
        best-effort path-based hardening, not cross-process serialization: a
        sub-``unlink`` window remains because the path, not an inode, is
        unlinked.
        """

        if not self._still_observed_stale_socket(identity):
            return

        if _probe_unix_socket(self._socket_path) is not _SocketLiveness.STALE:
            raise ControlSocketPathError(
                self._socket_path,
                "Control socket path changed during startup",
            )

        # Final check immediately before unlink: the re-probe above reconnected,
        # so re-confirm the node is still the same stale socket.
        if not self._still_observed_stale_socket(identity):
            return

        try:
            os.unlink(self._socket_path)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise ControlSocketPathError(
                self._socket_path,
                "Stale control socket could not be removed",
            ) from exc

    def _still_observed_stale_socket(self, identity: _SocketIdentity) -> bool:
        """Return whether the path is still exactly the observed stale socket.

        ``False`` means the path has since disappeared, which is safe: the
        caller can proceed to a fresh bind. A changed inode, a non-socket
        object, or an ambiguous ``lstat`` failure all fail closed instead.
        """

        try:
            current = os.lstat(self._socket_path)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise ControlSocketPathError(
                self._socket_path,
                "Control socket path cannot be inspected",
            ) from exc

        if (
            not stat.S_ISSOCK(current.st_mode)
            or (current.st_dev, current.st_ino) != identity
        ):
            raise ControlSocketPathError(
                self._socket_path,
                "Control socket path changed during startup",
            )

        return True


async def _write_response(writer, response: bytes) -> bool:
    try:
        writer.write(response + b"\n")
        await writer.drain()
    except (BrokenPipeError, ConnectionError):
        return False
    return True


async def _close_writer(writer) -> None:
    try:
        writer.close()
    except (BrokenPipeError, ConnectionError):
        return

    wait_closed = getattr(writer, "wait_closed", None)
    if wait_closed is None:
        return

    try:
        result = wait_closed()
        if inspect.isawaitable(result):
            await result
    except (BrokenPipeError, ConnectionError):
        return


def _socket_identity(path: str) -> _SocketIdentity | None:
    try:
        path_stat = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISSOCK(path_stat.st_mode):
        return None
    return (path_stat.st_dev, path_stat.st_ino)


def _bind_control_socket(path: str, socket_mode: int) -> socket.socket:
    """Return a bound, not-yet-listening ``AF_UNIX`` socket for ``path``.

    The filesystem socket node is created under a mode-derived umask so it can
    never appear on disk more permissively than ``socket_mode``, even briefly.
    No ``await`` occurs while that temporary umask is installed, and the
    original umask is restored even if ``bind`` fails. The caller owns the
    returned socket until it is handed to ``asyncio.start_unix_server``.
    """

    raw_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        previous_umask = os.umask(0o777 & ~socket_mode)
        try:
            raw_socket.bind(path)
        finally:
            os.umask(previous_umask)
    except BaseException:
        raw_socket.close()
        raise
    return raw_socket


def _probe_unix_socket(path: str) -> _SocketLiveness:
    """Classify an existing Unix socket path without sending a protocol request.

    A completed connection means a listener is alive; ``ECONNREFUSED`` (or the
    path already being gone) means the node is a stale leftover; anything else
    is treated as uncertain so the caller can fail closed.
    """

    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(_STALE_SOCKET_PROBE_TIMEOUT_SECONDS)
        try:
            probe.connect(path)
        except (ConnectionRefusedError, FileNotFoundError):
            return _SocketLiveness.STALE
        except OSError:
            return _SocketLiveness.UNCERTAIN
        return _SocketLiveness.LIVE
    finally:
        probe.close()


def _remove_matching_socket(
    path: str,
    identity: _SocketIdentity,
    *,
    log_errors: bool,
) -> None:
    try:
        path_stat = os.lstat(path)
    except FileNotFoundError:
        return

    if not stat.S_ISSOCK(path_stat.st_mode):
        return
    if (path_stat.st_dev, path_stat.st_ino) != identity:
        return

    try:
        os.unlink(path)
    except FileNotFoundError:
        return
    except OSError:
        if log_errors:
            logger.warning("Failed to remove routing control socket.", exc_info=True)
            return
        raise


def _path_type(mode: int) -> str:
    if stat.S_ISLNK(mode):
        return "symbolic link"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "regular file"
    if stat.S_ISFIFO(mode):
        return "FIFO"
    if stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
        return "device"
    return "non-socket filesystem object"
