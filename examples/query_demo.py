"""Run four offline read-only queries on a synthetic SQLite projection."""

from hashlib import sha256
import json
from pathlib import Path
import sys
from tempfile import gettempdir
from uuid import uuid4

EXAMPLES = Path(__file__).resolve().parent
sys.path.insert(0, str(EXAMPLES.parent / "source"))

from product_workflow.formal_150w_query import (  # noqa: E402
    DATABASE_REQUEST_SCHEMA,
    Formal150wQueryService,
    build_projection,
)


def request(query, kind, **fields):
    result = query.execute_request({
        "schema_version": DATABASE_REQUEST_SCHEMA,
        "kind": kind,
        **fields,
    })
    if result["status"] == "error":
        raise RuntimeError(f"Demo query failed: {result['error']['code']}")
    return result["data"]


def main():
    source = EXAMPLES / "synthetic_query_records.csv"
    source_sha = sha256(source.read_bytes()).hexdigest()
    directory = Path(gettempdir()) / f"bonemetgpt-query-demo-{uuid4().hex}"
    directory.mkdir()
    database = directory / "demo.sqlite"
    manifest = directory / "demo.manifest.json"
    try:
        projection = build_projection(
            source, database, manifest, expected_source_sha256=source_sha,
        )
        query = Formal150wQueryService(
            database,
            manifest,
            expected_source_sha256=source_sha,
            expected_manifest_sha256=sha256(manifest.read_bytes()).hexdigest(),
            expected_projection_sha256=projection["projection"]["sha256"],
        )
        low_density = [{"field": "relative_density", "op": "lt", "value": 0.20}]
        rows = request(
            query, "records", filters=low_density,
            order_by=[{"field": "C11", "direction": "desc"}], limit=3,
        )
        empty = request(
            query, "records",
            filters=[{"field": "relative_density", "op": "lt", "value": 0.01}],
            order_by=[], limit=3,
        )
        report = {
            "data_source": "synthetic_demo",
            "eligible_rows": request(query, "overview")["eligible_rows"],
            "density_below_20_percent": request(query, "count", filters=low_density)["matched_count"],
            "highest_c11_below_20_percent": rows["rows"],
            "no_match_rows": empty["rows"],
        }
    finally:
        manifest.unlink(missing_ok=True)
        database.unlink(missing_ok=True)
        directory.rmdir()
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

