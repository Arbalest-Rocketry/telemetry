"""Step 3: silver -> gold.

Computes one summary row per test (peak pressure, burn time, ...) and stores it
for cross-test dashboards. Re-running must replace the row, not duplicate it.
"""


def build_gold(run_id: str) -> None:
    raise NotImplementedError
