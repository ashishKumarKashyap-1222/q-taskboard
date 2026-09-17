"""
Test double for the real Airtable integration in airtable_client.py.

Use this in unit tests only - never import it from production code. It stands
in for a pyairtable Table, implementing just enough of the interface
(`batch_upsert`) that `airtable_client.export_tasks()` calls, so tests can
exercise retry/fallback/idempotency behavior without making real network
calls or depending on a real Airtable base being reachable.
"""
import requests


class MockAirtableError(requests.exceptions.HTTPError):
    """A fake HTTPError carrying a real status code, like pyairtable raises."""

    def __init__(self, status_code, message='mock airtable error'):
        response = requests.Response()
        response.status_code = status_code
        super().__init__(message, response=response)


class MockAirtableTable:
    """
    In-memory stand-in for a pyairtable Table.

    records_by_key: the fake "database", keyed by the upsert key field value -
        inspect this after calling export_tasks() to assert what would have
        landed in Airtable (and that re-running doesn't duplicate rows).
    fail_task_ids: Task ID values that should always fail with a permanent
        (422) error, simulating one bad record in an otherwise-fine batch.
    transient_failures: number of calls that should fail with a transient
        (503) error before starting to succeed, simulating a flaky API that
        recovers on retry.
    calls: list of the field-dicts passed to each batch_upsert call, for
        assertions on chunking/retry behavior.
    """

    def __init__(self, fail_task_ids=None, transient_failures=0):
        self.records_by_key = {}
        self.fail_task_ids = set(fail_task_ids or ())
        self.transient_failures = transient_failures
        self.calls = []

    def batch_upsert(self, records, key_fields):
        self.calls.append([r['fields'] for r in records])

        if self.transient_failures > 0:
            self.transient_failures -= 1
            raise MockAirtableError(503, 'mock transient failure')

        key_field = key_fields[0]
        for record in records:
            key_value = record['fields'][key_field]
            if key_value in self.fail_task_ids:
                raise MockAirtableError(422, f'mock permanent failure for {key_value}')

        for record in records:
            key_value = record['fields'][key_field]
            self.records_by_key[key_value] = record['fields']

        return {'createdRecords': [], 'updatedRecords': [], 'records': records}
