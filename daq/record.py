import argparse
from itertools import islice
from math import isfinite
from pathlib import Path
from uuid import uuid4

from daq.acquire.mock import MockSource
from daq.log.writer import DiskLogger


def main() -> None:
    parser = argparse.ArgumentParser(description="Record raw DAQ voltages to disk.")
    parser.add_argument("--mock", action="store_true", required=True)
    parser.add_argument("--seconds", type=float, default=2.0)
    parser.add_argument("--out", type=Path, default=Path("data/runs"))
    args = parser.parse_args()
    source = MockSource()
    if not isfinite(args.seconds) or args.seconds * source.sample_rate_hz < 1:
        parser.error("--seconds must be finite and at least one sample long")
    run_id = uuid4().hex
    run_dir = args.out / run_id
    with DiskLogger(run_dir, run_id, source.start_utc, source.sample_rate_hz) as logger:
        try:
            for scan in islice(source.stream(), int(args.seconds * source.sample_rate_hz)):
                logger.write(scan)
        except KeyboardInterrupt:
            pass
    print(run_dir)


if __name__ == "__main__":
    main()
