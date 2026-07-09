import queue
import socket
import threading


LEGACY_UDP_INPUT_TYPE = "udp"
SERIAL_INPUT_TYPE = "serial"

DEFAULT_SERIAL_INPUT = {
    "baudrate": 38400,
    "bytesize": 8,
    "parity": "N",
    "stopbits": 1,
    "read_timeout": 1.0,
    "reconnect_delay": 5,
    "max_line_bytes": 4096,
}

DEFAULT_SERIAL_QUEUE_LINES = 256
DEFAULT_SERIAL_POLL_INTERVAL = 0.2
SERIAL_READ_CHUNK_BYTES = 1024
SUPPORTED_BYTESIZES = {5, 6, 7, 8}
SUPPORTED_PARITIES = {"N", "E", "O", "M", "S"}
SUPPORTED_STOPBITS = {1, 1.5, 2}


class InputConfigError(ValueError):
    """Raised for operator-facing local input configuration errors."""


def load_default_serial_factory():
    try:
        import serial
    except ImportError as exc:
        raise InputConfigError(
            "input.type: serial requires pySerial; install the pyserial package"
        ) from exc
    return serial.Serial


class _UnrestrictedPolicy:
    def allows(self, _address):
        return True


def _explicit_value(mapping, key, default=None, required=False):
    if key not in mapping:
        if required:
            raise InputConfigError(f"input.{key}: required")
        return default
    value = mapping[key]
    if value is None:
        raise InputConfigError(f"input.{key}: explicit null is not supported")
    return value


def _positive_int(value, context):
    if isinstance(value, bool) or not isinstance(value, int):
        raise InputConfigError(f"{context}: must be a positive integer")
    if value <= 0:
        raise InputConfigError(f"{context}: must be a positive integer")
    return value


def _positive_float(value, context):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InputConfigError(f"{context}: must be a positive number")
    value = float(value)
    if value <= 0:
        raise InputConfigError(f"{context}: must be a positive number")
    return value


def _bytesize(value):
    value = _positive_int(value, "input.bytesize")
    if value not in SUPPORTED_BYTESIZES:
        raise InputConfigError(
            "input.bytesize: supported values are 5, 6, 7, and 8"
        )
    return value


def _parity(value):
    if not isinstance(value, str):
        raise InputConfigError("input.parity: must be a string")
    normalized = value.strip().upper()
    if normalized not in SUPPORTED_PARITIES:
        raise InputConfigError(
            "input.parity: supported values are N, E, O, M, and S"
        )
    return normalized


def _stopbits(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InputConfigError("input.stopbits: must be 1, 1.5, or 2")
    normalized = float(value)
    if normalized not in SUPPORTED_STOPBITS:
        raise InputConfigError("input.stopbits: must be 1, 1.5, or 2")
    if normalized == 1:
        return 1
    if normalized == 2:
        return 2
    return 1.5


def validate_serial_input_config(raw_input):
    if not isinstance(raw_input, dict):
        raise InputConfigError("input: must be a mapping")

    allowed_keys = set(DEFAULT_SERIAL_INPUT) | {"type", "port"}
    unknown_keys = sorted(set(raw_input) - allowed_keys)
    if unknown_keys:
        names = ", ".join(f"input.{key}" for key in unknown_keys)
        raise InputConfigError(f"{names}: unknown serial input option")

    port = _explicit_value(raw_input, "port", required=True)
    if not isinstance(port, str) or not port.strip():
        raise InputConfigError("input.port: must be a non-empty string")

    baudrate = _positive_int(
        _explicit_value(
            raw_input,
            "baudrate",
            DEFAULT_SERIAL_INPUT["baudrate"],
        ),
        "input.baudrate",
    )
    max_line_bytes = _positive_int(
        _explicit_value(
            raw_input,
            "max_line_bytes",
            DEFAULT_SERIAL_INPUT["max_line_bytes"],
        ),
        "input.max_line_bytes",
    )

    return {
        "type": SERIAL_INPUT_TYPE,
        "port": port,
        "baudrate": baudrate,
        "bytesize": _bytesize(
            _explicit_value(
                raw_input,
                "bytesize",
                DEFAULT_SERIAL_INPUT["bytesize"],
            )
        ),
        "parity": _parity(
            _explicit_value(
                raw_input,
                "parity",
                DEFAULT_SERIAL_INPUT["parity"],
            )
        ),
        "stopbits": _stopbits(
            _explicit_value(
                raw_input,
                "stopbits",
                DEFAULT_SERIAL_INPUT["stopbits"],
            )
        ),
        "read_timeout": _positive_float(
            _explicit_value(
                raw_input,
                "read_timeout",
                DEFAULT_SERIAL_INPUT["read_timeout"],
            ),
            "input.read_timeout",
        ),
        "reconnect_delay": _positive_float(
            _explicit_value(
                raw_input,
                "reconnect_delay",
                DEFAULT_SERIAL_INPUT["reconnect_delay"],
            ),
            "input.reconnect_delay",
        ),
        "max_line_bytes": max_line_bytes,
    }


def normalize_local_input_config(config):
    if "input" not in config:
        return {"type": LEGACY_UDP_INPUT_TYPE}

    raw_input = config["input"]
    if raw_input is None:
        raise InputConfigError("input: explicit null is not supported")
    if not isinstance(raw_input, dict):
        raise InputConfigError("input: must be a mapping")

    raw_type = _explicit_value(raw_input, "type", required=True)
    if not isinstance(raw_type, str):
        raise InputConfigError("input.type: must be 'serial'")
    input_type = raw_type.strip().lower()
    if input_type != SERIAL_INPUT_TYPE:
        raise InputConfigError(
            f"input.type: unsupported value {raw_type!r}; supported value is 'serial'"
        )

    if "allow_from" in config:
        raise InputConfigError(
            "allow_from applies only to the legacy UDP input and cannot be "
            "used with input.type: serial"
        )

    return validate_serial_input_config(raw_input)


class SerialLineFramer:
    def __init__(self, max_line_bytes):
        self.max_line_bytes = _positive_int(max_line_bytes, "input.max_line_bytes")
        self._buffer = bytearray()
        self._discarding_overlong = False
        self._previous_was_cr = False

    @property
    def buffer_size(self):
        return len(self._buffer)

    def reset(self):
        self._buffer.clear()
        self._discarding_overlong = False
        self._previous_was_cr = False

    def feed(self, chunk):
        lines = []
        overlong_dropped = 0
        for byte in bytes(chunk):
            if self._previous_was_cr:
                self._previous_was_cr = False
                if byte == 0x0A:
                    continue

            if byte == 0x0D:
                line = self._finish_line()
                if line is not None:
                    lines.append(line)
                self._previous_was_cr = True
            elif byte == 0x0A:
                line = self._finish_line()
                if line is not None:
                    lines.append(line)
            elif self._discarding_overlong:
                continue
            elif len(self._buffer) >= self.max_line_bytes:
                self._buffer.clear()
                self._discarding_overlong = True
                overlong_dropped += 1
            else:
                self._buffer.append(byte)
        return lines, overlong_dropped

    def _finish_line(self):
        if self._discarding_overlong:
            self._buffer.clear()
            self._discarding_overlong = False
            return None
        line = bytes(self._buffer)
        self._buffer.clear()
        return line


class UdpInputAdapter:
    def __init__(self, sock, ingress_policy=None, owns_socket=False):
        self.sock = sock
        self.ingress_policy = ingress_policy or _UnrestrictedPolicy()
        self.owns_socket = owns_socket

    @classmethod
    def bind(cls, config, ingress_policy=None):
        udp_family = (
            socket.AF_INET6 if ":" in config["listen_ip"] else socket.AF_INET
        )
        sock = socket.socket(udp_family, socket.SOCK_DGRAM)
        sock.bind((config["listen_ip"], config["listen_port"]))
        return cls(sock, ingress_policy, owns_socket=True)

    def start(self):
        return None

    def close(self):
        if not self.owns_socket:
            return
        close = getattr(self.sock, "close", None)
        if close:
            close()

    def selectable_sockets(self):
        return [self.sock]

    def poll_interval(self):
        return None

    def read_ready(self, ready_socket):
        if ready_socket is not self.sock:
            return []
        data, addr = self.sock.recvfrom(4096)
        if not self.ingress_policy.allows(addr[0]):
            return []
        return [data]

    def read_pending(self):
        return []


class SerialInputAdapter:
    def __init__(
        self,
        settings,
        serial_factory=None,
        queue_max_lines=DEFAULT_SERIAL_QUEUE_LINES,
        poll_interval=DEFAULT_SERIAL_POLL_INTERVAL,
        logger=None,
    ):
        self.settings = validate_serial_input_config(settings)
        self.serial_factory = serial_factory or load_default_serial_factory()
        self.queue = queue.Queue(maxsize=_positive_int(
            queue_max_lines, "serial queue size"
        ))
        self._poll_interval = _positive_float(
            poll_interval,
            "serial poll interval",
        )
        self._logger = logger
        self._framer = SerialLineFramer(self.settings["max_line_bytes"])
        self._stop_event = threading.Event()
        self._thread = None
        self._serial_lock = threading.Lock()
        self._serial = None
        self.dropped_queue_lines = 0
        self.dropped_overlong_lines = 0

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="nmea_sproxy_serial_reader",
            daemon=True,
        )
        self._thread.start()

    def close(self):
        self._stop_event.set()
        self._close_serial()
        if self._thread:
            self._thread.join(timeout=self.settings["read_timeout"] + 1.0)

    def selectable_sockets(self):
        return []

    def poll_interval(self):
        return self._poll_interval

    def read_ready(self, _ready_socket):
        return []

    def read_pending(self):
        lines = []
        while True:
            try:
                lines.append(self.queue.get_nowait())
            except queue.Empty:
                break
        return lines

    def _run(self):
        while not self._stop_event.is_set():
            serial_obj = None
            try:
                serial_obj = self._open_serial()
                with self._serial_lock:
                    self._serial = serial_obj
                self._log(
                    "Serial input opened on "
                    f"{self.settings['port']} at {self.settings['baudrate']} baud."
                )
                self._read_until_error(serial_obj)
            except Exception as exc:
                if not self._stop_event.is_set():
                    self._log(
                        "Serial input unavailable on "
                        f"{self.settings['port']}: {exc}. "
                        f"Retrying in {self.settings['reconnect_delay']} seconds."
                    )
            finally:
                self._close_serial_object(serial_obj)
                with self._serial_lock:
                    if self._serial is serial_obj:
                        self._serial = None
                self._framer.reset()

            if not self._stop_event.is_set():
                self._stop_event.wait(self.settings["reconnect_delay"])

    def _open_serial(self):
        return self.serial_factory(
            port=self.settings["port"],
            baudrate=self.settings["baudrate"],
            bytesize=self.settings["bytesize"],
            parity=self.settings["parity"],
            stopbits=self.settings["stopbits"],
            timeout=self.settings["read_timeout"],
        )

    def _read_until_error(self, serial_obj):
        while not self._stop_event.is_set():
            chunk = serial_obj.read(SERIAL_READ_CHUNK_BYTES)
            if not chunk:
                continue
            lines, overlong_dropped = self._framer.feed(chunk)
            if overlong_dropped:
                self._warn_overlong(overlong_dropped)
            for line in lines:
                self._enqueue_line(line)

    def _enqueue_line(self, line):
        try:
            self.queue.put_nowait(line)
            return
        except queue.Full:
            self._drop_oldest_queued_line()

        try:
            self.queue.put_nowait(line)
        except queue.Full:
            self._drop_oldest_queued_line()

    def _drop_oldest_queued_line(self):
        try:
            self.queue.get_nowait()
        except queue.Empty:
            pass
        self.dropped_queue_lines += 1
        if self._should_log_drop(self.dropped_queue_lines):
            self._log(
                "Serial input queue full; dropped oldest queued line "
                f"({self.dropped_queue_lines} total)."
            )

    def _warn_overlong(self, count):
        self.dropped_overlong_lines += count
        if self._should_log_drop(self.dropped_overlong_lines):
            self._log(
                "Serial input discarded an overlong line "
                f"({self.dropped_overlong_lines} total)."
            )

    def _should_log_drop(self, count):
        return count == 1 or count % 100 == 0

    def _close_serial(self):
        with self._serial_lock:
            serial_obj = self._serial
        self._close_serial_object(serial_obj)

    def _close_serial_object(self, serial_obj):
        if serial_obj is None:
            return
        close = getattr(serial_obj, "close", None)
        if not close:
            return
        try:
            close()
        except Exception:
            pass

    def _log(self, message):
        if self._logger:
            self._logger(message)
        else:
            print(message)
