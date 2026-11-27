"""Counterfactual checks for the real bound SQLite reader using only synthetic rows."""
import csv
import hashlib
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from product_workflow import formal_150w_query as db
from tests.test_product_workflow_targets import local_test_directory


class FollowupQuerySQLTests(unittest.TestCase):
    def test_threshold_order_tie_and_limit_change_actual_returned_rows(self):
        with local_test_directory() as root:
            csv_file, projection, manifest = (root / n for n in ('synthetic.csv', 'db.sqlite', 'manifest.json'))
            flags = {'is_singular': 0, 'is_pd_full': 1, 'has_periodic_mismatch': 0, 'is_degenerate': 0}
            with csv_file.open('x', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=[*db.PROJECTION_COLUMNS, *flags])
                writer.writeheader()
                for sample, c11 in [(35, .25), (11, .0625), (33, .25), (22, .125)]:
                    writer.writerow({'sample_id': sample, 'relative_density': .2, 'diameter': .1,
                                     **dict.fromkeys(db.STIFFNESS_NAMES, c11), **flags})
            digest = hashlib.sha256(csv_file.read_bytes()).hexdigest().upper()
            db.build_projection(csv_file, projection, manifest, expected_source_sha256=digest)
            before = [p.read_bytes() for p in (csv_file, projection, manifest)]
            service = db.Formal150wQueryService(projection, manifest, expected_source_sha256=digest)
            evidence = []
            with patch.object(service, '_execute_read', wraps=service._execute_read) as execute:
                for threshold, direction, limit, expected in (
                    (.125, 'desc', 5, [33, 35, 22]), (.125, 'asc', 5, [22, 33, 35]),
                    (.25, 'desc', 1, [33]), (.5, 'desc', 5, []), (0., 'asc', 5, [11, 22, 33, 35]),
                ):
                    with self.subTest(threshold=threshold, direction=direction, limit=limit):
                        request = {'schema_version': db.DATABASE_REQUEST_SCHEMA, 'kind': 'records',
                                   'filters': [{'field': 'C11', 'op': 'gte', 'value': threshold}],
                                   'order_by': [{'field': 'C11', 'direction': direction}], 'limit': limit}
                        result = service.execute_request(request)
                        ids = [row['sample_id'] for row in result['data']['rows']]
                        self.assertEqual(result['status'], 'ok' if expected else 'empty')
                        self.assertEqual(ids, expected)
                        sql, params = execute.call_args.args
                        self.assertIn('WHERE C11 >= ?', sql)
                        self.assertIn('ORDER BY C11 ' + direction.upper() + ', sample_id ASC LIMIT ?', sql)
                        self.assertEqual(params, [threshold, limit])
                        evidence.append(dict(request=request, sql=sql, parameters=params, ids=ids))
            self.assertEqual(before, [p.read_bytes() for p in (csv_file, projection, manifest)])
            if os.getenv('BMG_ACCEPTANCE_ARTIFACTS'):
                path = Path(os.environ['BMG_ACCEPTANCE_ARTIFACTS']) / 'query-sql-counterfactuals.json'
                with path.open('x', encoding='utf-8') as f:
                    json.dump(evidence, f, indent=2)

