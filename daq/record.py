import argparse
from contextlib import ExitStack
from itertools import islice
from math import isfinite
from pathlib import Path
from uuid import uuid4

from daq.acquire.mock import MockSource
from daq.live.client import LiveClient
from daq.log.writer import DiskLogger


def main() -> None:
    parser = argparse.ArgumentParser(description="Record raw DAQ voltages to disk.")
    parser.add_argument("--mock", action="store_true", required=True)
    parser.add_argument("--seconds", type=float, default=2.0)
    parser.add_argument("--out", type=Path, default=Path("data/runs"))
    parser.add_argument("--ingest-url", help="Ingest service base URL")
    parser.add_argument("--live-rate-hz", type=float, default=25.0)
    args = parser.parse_args()
    source = MockSource()
    if not isfinite(args.seconds) or args.seconds * source.sample_rate_hz < 1:
        parser.error("--seconds must be finite and at least one sample long")
    if not isfinite(args.live_rate_hz) or not 0 < args.live_rate_hz <= source.sample_rate_hz:
        parser.error("--live-rate-hz must be positive and no greater than the acquisition rate")
    run_id = uuid4().hex
    run_dir = args.out / run_id
    with ExitStack() as stack:
        live = stack.enter_context(LiveClient(
            args.ingest_url, run_id, source.sample_rate_hz, args.live_rate_hz,
        )) if args.ingest_url else None
        logger = stack.enter_context(DiskLogger(
            run_dir, run_id, source.start_utc, source.sample_rate_hz,
        ))
        try:
            for scan in islice(source.stream(), int(args.seconds * source.sample_rate_hz)):
                logger.write(scan)
                if live is not None:
                    live.write(scan)
        except KeyboardInterrupt:
            pass
    print(run_dir)


if __name__ == "__main__":
    main()
