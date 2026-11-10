import asyncio
import socket
from threading import Event, Thread

from pymodbus.server import ModbusTcpServer
from pymodbus.simulator import DataType, SimData, SimDevice

from daq.control.state import Mask, Valve


class FakePlc:
    """In-process Modbus TCP server standing in for the valve PLC."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0, unit: int = 1) -> None:
        self.host = host
        self.unit = unit
        if port == 0:
            with socket.socket() as probe:
                probe.bind((host, 0))
                port = probe.getsockname()[1]
        self.port = port
        # Valves that ignore open commands, to fake a readback mismatch.
        self.stuck_closed: set[Valve] = set()
        self._thread: Thread | None = None

    def start(self) -> None:
        self._ready = Event()
        self._thread = Thread(target=asyncio.run, args=(self._serve(),), name="fake-plc")
        self._thread.start()
        if not self._ready.wait(5):
            raise RuntimeError("Fake PLC did not start")

    async def _serve(self) -> None:
        self._loop = asyncio.get_running_loop()
        # Built fresh on each start: a rebooted PLC comes up with outputs off.
        bits = [SimData(0, count=16, values=False, datatype=DataType.BITS)]
        registers = [SimData(0, count=1, values=0, datatype=DataType.REGISTERS)]
        device = SimDevice(
            id=self.unit, simdata=(bits, bits, registers, registers), action=self._on_request,
        )
        self._server = ModbusTcpServer(device, address=(self.host, self.port))
        await self._server.serve_forever(background=True)
        self._ready.set()
        await self._server.serving

    async def _on_request(
        self, function_code: int, start_address: int, address: int, count: int,
        current_registers: list[int], set_values: list[int] | list[bool] | None,
    ) -> None:
        if set_values is not None:
            for valve in self.stuck_closed:
                if address <= valve < address + len(set_values):
                    set_values[valve - address] = False

    def stop(self) -> None:
        if self._thread is None:
            return
        asyncio.run_coroutine_threadsafe(self._server.shutdown(), self._loop).result(5)
        self._thread.join()
        self._thread = None

    @property
    def coils(self) -> Mask:
        read = self._server.async_getValues(self.unit, 1, 0, len(Valve))
        return tuple(bool(bit) for bit in asyncio.run_coroutine_threadsafe(read, self._loop).result(5))
