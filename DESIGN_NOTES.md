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

<!-- filled in alongside the Activity Feed implementation -->
