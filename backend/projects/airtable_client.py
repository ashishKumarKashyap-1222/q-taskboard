"""
Real Airtable integration for Part 3c (bulk task export).

Uses pyairtable's batch_upsert, keyed on a "Task ID" field, so re-running an
export updates existing rows instead of creating duplicates (handles being
run more than once gracefully).

Error handling: a chunk of records is retried on transient failures (429
rate-limit, 5xx, network errors) with exponential backoff. If a chunk still
fails after retries - most likely because ONE record in it is permanently
invalid (400/401/403/404/422) - we fall back to submitting that chunk's
records one at a time, so a single bad record is recorded as a failure and
skipped instead of taking the rest of the batch down with it. Permanent
failures are never retried.
"""
import logging
import os
import time

from requests.exceptions import RequestException

from pyairtable import Api

logger = logging.getLogger(__name__)

CHUNK_SIZE = 10
MAX_ATTEMPTS = 3
BASE_BACKOFF_SECONDS = 1
TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}
KEY_FIELD = 'Task ID'


class AirtableConfigError(Exception):
    """Airtable credentials/config are missing - export can't proceed at all."""


def _get_table():
    api_key = os.environ.get('AIRTABLE_API_KEY')
    base_id = os.environ.get('AIRTABLE_BASE_ID')
    table_name = os.environ.get('AIRTABLE_TABLE_NAME')
    if not (api_key and base_id and table_name):
        raise AirtableConfigError(
            'Airtable is not configured (missing AIRTABLE_API_KEY / AIRTABLE_BASE_ID / AIRTABLE_TABLE_NAME)'
        )
    return Api(api_key).table(base_id, table_name)


def _is_transient(exc):
    response = getattr(exc, 'response', None)
    status_code = getattr(response, 'status_code', None)
    if status_code is not None:
        return status_code in TRANSIENT_STATUS_CODES
    # No HTTP response at all (timeout, connection error) - worth retrying.
    return isinstance(exc, RequestException)


def _task_to_record(task):
    return {
        'fields': {
            KEY_FIELD: str(task.id),
            'Title': task.title,
            'Description': task.description or '',
            'Assignee': task.assignee.name if task.assignee_id else '',
            'Status': task.status,
            'Project': task.project.name,
            'Position': task.position,
        }
    }


def _upsert_with_retry(table, records):
    """Attempt one batch_upsert call, retrying transient failures. Returns (ok, error)."""
    attempt = 0
    while True:
        attempt += 1
        try:
            table.batch_upsert(records, key_fields=[KEY_FIELD])
            return True, None
        except Exception as exc:
            if _is_transient(exc) and attempt < MAX_ATTEMPTS:
                backoff = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
                logger.warning('Airtable batch upsert failed (attempt %d/%d), retrying in %ds: %s',
                                attempt, MAX_ATTEMPTS, backoff, exc)
                time.sleep(backoff)
                continue
            return False, exc


def export_tasks(tasks, table=None):
    """
    Upsert the given tasks into Airtable. Returns a summary dict:
        {'exported': int, 'failed': [{'task_id': str, 'title': str, 'error': str}, ...]}

    Never raises for a single bad record - failures are collected and the rest
    of the export continues. Raises AirtableConfigError if credentials are
    missing entirely (nothing to fall back to in that case).

    `table` is injectable for tests (see airtable_mock.py); production code
    should call this with no `table` argument so it hits the real API.
    """
    table = table or _get_table()
    tasks = list(tasks)
    exported = 0
    failed = []

    for i in range(0, len(tasks), CHUNK_SIZE):
        chunk = tasks[i:i + CHUNK_SIZE]
        records = [_task_to_record(t) for t in chunk]

        ok, exc = _upsert_with_retry(table, records)
        if ok:
            exported += len(chunk)
            continue

        # Chunk failed even after retries - isolate the bad record(s) instead of
        # failing the whole chunk.
        for task, record in zip(chunk, records):
            ok, exc = _upsert_with_retry(table, [record])
            if ok:
                exported += 1
            else:
                logger.error('Airtable export permanently failed for task %s: %s', task.id, exc)
                failed.append({'task_id': str(task.id), 'title': task.title, 'error': str(exc)})

    return {'exported': exported, 'failed': failed}
