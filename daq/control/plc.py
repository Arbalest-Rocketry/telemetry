import logging
from threading import Event, Lock, Thread

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException
from pymodbus.pdu import ModbusPDU

from daq.control.config import ControlConfig
from daq.control.state import Mask, PATTERNS, State, Valve

# pymodbus logs every refused connect; status carries that instead.
logging.getLogger("pymodbus").setLevel(logging.CRITICAL)


def _checked(reply: ModbusPDU) -> ModbusPDU:
    if reply.isError():
        raise ModbusException(str(reply))
    return reply


class ValveClient:
    """Writes the latest valve mask to the PLC coils and reads them back."""

    def __init__(
        self, config: ControlConfig = ControlConfig(), *,
        timeout_s: float = 0.5, poll_s: float = 0.2,
    ) -> None:
        self.config = config
        self.timeout_s = timeout_s
        self.poll_s = poll_s
        self.commanded: Mask = PATTERNS[State.IDLE]
        self.confirmed: Mask | None = None
        self.connected = False
        self.status = "PLC connecting"
        self._unsent = False
        self._lock = Lock()
        self._wake = Event()
        self._stop = Event()
        self._thread = Thread(target=self._run, name="daq-plc")
        self._thread.start()

    def set_mask(self, mask: Mask) -> None:
        """Never waits on the PLC; only the newest mask is kept."""
        with self._lock:
            self.commanded = mask
            self._unsent = True
        self._wake.set()

    def _run(self) -> None:
        client = ModbusTcpClient(
            self.config.plc_host, port=self.config.plc_port, timeout=self.timeout_s, retries=0,
        )
        unit = self.config.plc_unit
        while not self._stop.is_set():
            with self._lock:
                mask, unsent = self.commanded, self._unsent
                self._unsent = False
            try:
                if not client.connected:
                    if not client.connect():
                        raise ConnectionError("PLC unreachable")
                    # A PLC that just came up may not hold the mask we last sent.
                    write = True
                else:
                    write = unsent
                if write:
                    _checked(client.write_coils(0, list(mask), device_id=unit))
                reply = _checked(client.read_coils(0, count=len(Valve), device_id=unit))
                self.confirmed = tuple(reply.bits[:len(Valve)])
                self.connected = True
                self.status = "PLC ok" if self.confirmed == mask else "PLC readback mismatch"
            except (ModbusException, OSError) as error:
                client.close()
                self.connected = False
                self.confirmed = None
                if unsent:
                    with self._lock:
                        self._unsent = True
                    self.status = f"PLC write failed, latest mask queued: {error}"
                else:
                    self.status = f"PLC disconnected: {error}"
            self._wake.wait(self.poll_s)
            self._wake.clear()
        client.close()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        self._thread.join()
