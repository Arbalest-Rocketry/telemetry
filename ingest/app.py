"""Ingest service: runs on the site laptop.

Receives live batches from sources (POST /ingest), validates them against the
contract, and writes them to TimescaleDB. Must never be required for a source
to keep logging.
"""


def handle_ingest(message: dict) -> None:
    """Validate one live message and store its samples."""
    raise NotImplementedError


def main() -> None:
    """Start the HTTP server."""
    raise NotImplementedError
