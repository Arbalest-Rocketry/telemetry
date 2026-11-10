"""TimescaleDB writes for the ingest service. Table definitions are in db/schema.sql."""


def insert_samples(source: str, run_id: str, samples: list[dict]) -> None:
    raise NotImplementedError
