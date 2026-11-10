"""Step 1 after a test: copy a source's run folder into bronze, unchanged.

Checks that meta.json and raw.csv exist and follow CONTRACT.md (section 1).
Destination: bronze/<run_id>/<source>/
"""


def upload_run(run_folder: str) -> None:
    raise NotImplementedError
