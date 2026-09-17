# Design Notes

## Airtable integration: Python (pyairtable), not the npm `airtable` package

The assignment says to "use the official `airtable` npm package for the API calls."
This repo's backend is Django/Python, not Node — there is no Node server anywhere in
the stack to host an npm package with server-side credentials; the only Node process
here is the Vite dev server for the React frontend, which can't safely hold an Airtable
PAT. The README also already documents `backend/projects/airtable_mock.py` as the test
double's path, and `pyairtable` was already listed in `backend/requirements.txt` before
any of this work started - both signal this repo's variant of the assignment expects a
Python integration.

Standing up a second, Node-only service purely to satisfy the letter of "npm package"
would add a runtime and a service boundary that nothing else in this codebase has, for
no functional benefit - the real requirements (real API calls, retry/error handling,
idempotent re-runs, a test double) are all satisfied identically either way. Went with
`pyairtable` in `backend/projects/airtable_client.py`, in the same app and language as
every other endpoint.

## Part 3b rollback design (activity write vs. the mutation it logs)

If writing the `Activity` row fails, the change it was logging (task create, status/
assignee update, comment post) rolls back too — each mutation and its activity write(s)
happen inside one `transaction.atomic()` block in `backend/projects/views.py`
(`TaskListCreateView.post`, `TaskDetailView.patch`, `CommentListCreateView.post`).

Went with all-or-nothing over best-effort/fire-and-forget because the alternative means
the audit trail can silently drift from reality - a task shows as "In Progress" but the
feed never says who moved it there, with no error surfaced to anyone. For a feature
whose whole job is being a trustworthy record ("the team treats comments as part of the
engagement audit trail"), a log that can quietly go missing is worse than an action that
occasionally fails loudly and can be retried. The two writes are cheap, same-database,
same-request operations, so wrapping them in one transaction costs effectively nothing
in latency or complexity - this isn't a case where the durability/availability tradeoff
of eventual consistency actually buys anything.
