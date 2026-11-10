from dataclasses import dataclass, field
from typing import Protocol

from ingest.contract import LiveBatch, LiveChannel


class SampleStore(Protocol):
    def insert_batch(self, batch: LiveBatch) -> None: ...


@dataclass
class InMemoryStore:
    batches: list[LiveBatch] = field(default_factory=list, init=False)
    _channels: dict[tuple[str, str, str], LiveChannel] = field(
        default_factory=dict, init=False, repr=False,
    )

    def insert_batch(self, batch: LiveBatch) -> None:
        metadata = {
            (batch.source, batch.run_id, name): channel
            for name, channel in batch.channels.items()
        }
        for key, channel in metadata.items():
            if key in self._channels and self._channels[key] != channel:
                raise ValueError(f"unit/calibration_id changed for channel {key[2]}")
        self._channels.update(metadata)
        self.batches.append(batch)
