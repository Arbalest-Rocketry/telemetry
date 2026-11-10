from dataclasses import dataclass
from datetime import datetime, timedelta
from math import isfinite
import re


CONTRACT_VERSION = 1


@dataclass(frozen=True)
class LiveChannel:
    unit: str
    calibration_id: str | None


@dataclass(frozen=True)
class LiveSample:
    t: datetime
    channel: str
    value: float | int | None


@dataclass(frozen=True)
class LiveBatch:
    source: str
    run_id: str
    channels: dict[str, LiveChannel]
    samples: tuple[LiveSample, ...]


def _object(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


def _name(value: object, name: str) -> str:
    value = _text(value, name)
    if re.fullmatch(r"[a-z][a-z0-9_]*", value) is None:
        raise ValueError(f"{name} must be a lowercase identifier")
    return value


def validate_message(message: object) -> LiveBatch:
    message = _object(message, "batch")
    version = message.get("contract_version")
    if type(version) is not int or version != CONTRACT_VERSION:
        raise ValueError("contract_version must be 1")
    source = _name(message.get("source"), "source")
    run_id = _text(message.get("run_id"), "run_id")

    channels = {}
    metadata = _object(message.get("channels"), "channels")
    if not metadata:
        raise ValueError("channels must not be empty")
    for name, details in metadata.items():
        name = _name(name, "channel")
        details = _object(details, f"channels.{name}")
        unit = _text(details.get("unit"), f"channels.{name}.unit")
        if "calibration_id" not in details:
            raise ValueError(f"channels.{name}.calibration_id is required")
        calibration_id = details["calibration_id"]
        if calibration_id is not None:
            calibration_id = _text(calibration_id, f"channels.{name}.calibration_id")
        channels[name] = LiveChannel(unit, calibration_id)

    entries = message.get("samples")
    if not isinstance(entries, list) or not entries:
        raise ValueError("samples must be a nonempty list")
    samples = []
    for entry in entries:
        entry = _object(entry, "sample")
        channel = _name(entry.get("channel"), "sample.channel")
        if channel not in channels:
            raise ValueError(f"sample channel {channel} is missing from channels")
        timestamp = _text(entry.get("t"), "sample.t")
        try:
            t = datetime.fromisoformat(timestamp)
        except ValueError:
            raise ValueError("sample.t must be an ISO 8601 UTC timestamp") from None
        if t.utcoffset() != timedelta(0):
            raise ValueError("sample.t must be a UTC timestamp")
        if "value" not in entry:
            raise ValueError("sample.value is required (use null for missing readings)")
        value = entry["value"]
        if value is not None and (
            type(value) not in (int, float)
            or (isinstance(value, float) and not isfinite(value))
        ):
            raise ValueError("sample.value must be a finite number or null")
        samples.append(LiveSample(t, channel, value))

    return LiveBatch(source, run_id, channels, tuple(samples))
