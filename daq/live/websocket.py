from dataclasses import dataclass
import json
from math import isfinite
from queue import Empty

from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

from daq.live.client import LiveClient


@dataclass
class WebSocketClient(LiveClient):
    reconnect_delay_s: float = 0.1
    max_reconnect_delay_s: float = 5.0

    def __post_init__(self) -> None:
        if (not isfinite(self.reconnect_delay_s)
                or not isfinite(self.max_reconnect_delay_s)
                or not 0 < self.reconnect_delay_s <= self.max_reconnect_delay_s):
            raise ValueError("Reconnect delays must be finite, positive and ordered")
        super().__post_init__()

    def _run(self) -> None:
        url = self.ingest_url.rstrip("/")
        if url.startswith(("http://", "https://")):
            url = "ws" + url[4:]
        delay = self.reconnect_delay_s
        while not self._stop.is_set():
            batch = None
            try:
                with connect(
                    url + "/ingest/ws", open_timeout=self.timeout_s,
                    close_timeout=self.timeout_s, proxy=None,
                ) as websocket:
                    while not self._stop.is_set():
                        try:
                            batch = self._queue.get(timeout=0.05)
                        except Empty:
                            if self._closing.is_set():
                                return
                            continue
                        websocket.send(json.dumps(self._payload(batch), allow_nan=False))
                        response = json.loads(websocket.recv(timeout=self.timeout_s))
                        if response == {"accepted": len(batch) * len(self.channels)}:
                            self.sent_batches += 1
                            delay = self.reconnect_delay_s
                        else:
                            self.failed_batches += 1
                        batch = None
            except (OSError, TimeoutError, WebSocketException, ValueError):
                if batch is not None:
                    self.failed_batches += 1
            if self._stop.wait(delay):
                return
            delay = min(delay * 2, self.max_reconnect_delay_s)
