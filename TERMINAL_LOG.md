# Terminal Log

Assembled from the full recorded session (raw transcript captured via `script -a
terminal_log.txt`; video in the Loom recording linked from `RECORDING.md`). Organized
into the order the assignment asks for; some verbose/repeated pytest deprecation
warnings and unrelated local Docker containers (other projects on the same machine)
are trimmed for readability — nothing substantive is omitted.

## 1. Setup output

```
$ docker ps --format "table {{.Names}}\t{{.Status}}"
NAMES                STATUS
project-frontend-1   Up 21 minutes
project-backend-1    Up 21 minutes
project-db-1         Up 22 minutes

$ curl -sS http://localhost:8000/api/health
{"ok": true}

$ docker compose exec backend python manage.py migrate
Operations to perform:
  Apply all migrations: auth, contenttypes, projects, users
Running migrations:
  No migrations to apply.

$ docker compose exec backend python manage.py seed
seeding...
seed complete.
login with any of these (password: password123):
  meera@taskboard.dev   — admin on Q3 Launch, Internal Tools
  arjun@taskboard.dev   — admin on Onboarding, member on Q3 Launch
  kavya@example.com     — member on Q3 Launch
  dev@example.com       — viewer on Q3 Launch
  lina@example.com      — member on Onboarding
```

## 2. Initial test run

```
$ docker compose exec backend python -m pytest
================================= test session starts =================================
platform linux -- Python 3.12.14, pytest-8.4.2, pluggy-1.6.0
django: version: 5.2.17, settings: taskboard.settings (from ini)
collected 15 items

projects/tests.py .......                                                       [ 46%]
users/tests.py ........                                                         [100%]

========================== 15 passed, 16 warnings in 19.13s ===========================

$ docker compose exec frontend npm test
> taskboard-frontend@1.0.0 test
> vitest run

 ✓ src/tests/schemas.test.ts (6)
 ✓ src/tests/TaskCard.test.tsx (3)

 Test Files  2 passed (2)
      Tests  9 passed (9)
   Duration  3.56s
```

## 3. Bug curl proof

**REVIEW.md issue #2 — broken access control on `PATCH /api/tasks/:id`** (fresh data
created for the demo — no seed data touched):

```
$ curl -sS -w "\nHTTP_STATUS:%{http_code}\n" $BASE/api/projects/$PROJECT_ID \
    -H "Authorization: Bearer $LINA_TOK"
{"error":"forbidden"}
HTTP_STATUS:403

$ # THE BUG: lina PATCHes a task in a project she can't even view
$ curl -sS -w "\nHTTP_STATUS:%{http_code}\n" -X PATCH $BASE/api/tasks/$TASK_ID \
    -H "Authorization: Bearer $LINA_TOK" -d '{"title":"PWNED by non-member lina"}'
{"task":{"id":"05efd440-7808-4679-90cc-f04cc1868349","project_id":"eafb63a6-1b3f-4a82-9053-afcead3f109f","title":"PWNED by non-member lina","description":null,"status":"todo","assignee_id":null,"created_by_id":"e9cc17a3-d2bc-4a7c-ba60-df6520c44900","position":0,"created_at":"2026-09-17T10:00:11.864334Z","updated_at":"2026-09-17T10:00:12.664757Z","assignee":null}}
HTTP_STATUS:200
```
A user with zero access to the project still successfully edited its task (expected `403`, got `200`).

**REVIEW.md issue #1 / Part 2 — SQL injection in task search (before fix):**

```
$ echo "kavya's only project (Q3 Launch): $Q3_ID"
kavya's only project (Q3 Launch): 2a3f1607-c3af-478c-bc38-c21ac7757c2f

$ # normal search, correctly scoped to Q3 Launch only
$ curl -sS -G "$BASE/api/projects/$Q3_ID/tasks" -H "Authorization: Bearer $KAVYA_TOK" \
    --data-urlencode "q=launch" | python3 -m json.tool
{
    "tasks": [
        { "id": "109412fb-...", "title": "Finalize launch date with marketing", ... }
    ]
}

$ # THE BUG: injected q leaks tasks from projects kavya is NOT a member of
$ curl -sS -G "$BASE/api/projects/$Q3_ID/tasks" -H "Authorization: Bearer $KAVYA_TOK" \
    --data-urlencode "q=nonexistent%') OR 1=1 -- " | python3 -m json.tool
{
    "tasks": [
        { "title": "Finalize launch date with marketing", "project_id": "2a3f1607-...", ... },
        { "title": "Draft press release", "project_id": "2a3f1607-...", ... },
        { "title": "Record demo video", "project_id": "2a3f1607-...", ... },
        { "title": "Set up analytics dashboards", "project_id": "2a3f1607-...", ... },
        { "title": "Prepare customer email blast", "project_id": "2a3f1607-...", ... },
        { "title": "Update pricing page copy", "project_id": "2a3f1607-...", ... },
        { "title": "QA the new signup flow end-to-end", "project_id": "2a3f1607-...", ... },
        { "title": "Map current onboarding funnel", "project_id": "95b0571f-...", ... },
        { "title": "Interview 5 recently-onboarded customers", "project_id": "95b0571f-...", ... },
        { "title": "Wireframe new welcome screens", "project_id": "95b0571f-...", ... },
        { "title": "Audit current onboarding emails", "project_id": "95b0571f-...", ... },
        { "title": "Define success metric (TTFV target)", "project_id": "95b0571f-...", ... },
        { "title": "PWNED by non-member lina", "project_id": "eafb63a6-...", ... }
    ]
}
```
13 tasks returned — every task across every project in the database — for a user
scoped to a single project's search endpoint (`project_id=95b0571f-...` is the
*Onboarding* project; kavya is not a member of it).

## 4. Fix curl proof

```
$ docker compose exec backend python -m pytest -v
...
projects/tests.py::TestTasks::test_search_matches_title_or_description PASSED
projects/tests.py::TestTasks::test_search_does_not_leak_tasks_from_other_projects PASSED
projects/tests.py::TestTasks::test_search_handles_sql_special_characters_safely PASSED
...
================================ 18 passed, 22 warnings in 24.63s ================================

$ # SAME payload as before, against the fixed endpoint
$ curl -sS -G "$BASE/api/projects/$Q3_ID/tasks" -H "Authorization: Bearer $KAVYA_TOK" \
    --data-urlencode "q=launch" | python3 -m json.tool
{
    "tasks": [
        {
            "id": "109412fb-...", "title": "Finalize launch date with marketing",
            "assignee": {"id": "e9cc17a3-...", "email": "meera@taskboard.dev", "name": "Meera Iyer"}
        }
    ]
}

$ curl -sS -G "$BASE/api/projects/$Q3_ID/tasks" -H "Authorization: Bearer $KAVYA_TOK" \
    --data-urlencode "q=nonexistent%') OR 1=1 -- " | python3 -m json.tool
{
    "tasks": []
}
```
Normal search still works (and now correctly includes the nested `assignee` object,
matching the unfiltered list endpoint's shape). The identical injection payload that
leaked 13 cross-project tasks before now returns an empty, safely-scoped result.

## 5. Part 3c — Airtable export demo

```
$ curl -sS -X POST "$BASE/api/projects/$Q3_ID/export" -H "Authorization: Bearer $MEERA_TOK" \
    | python3 -m json.tool
{
    "exported": 7,
    "failed": []
}

$ # run it again - same project, must update not duplicate
$ curl -sS -X POST "$BASE/api/projects/$Q3_ID/export" -H "Authorization: Bearer $MEERA_TOK" \
    | python3 -m json.tool
{
    "exported": 7,
    "failed": []
}

$ # verify against the real Airtable base
$ docker compose exec -T backend python -c "... table.all() ..."
Total records in Airtable base: 7

$ # viewer cannot trigger export
$ curl -sS -w "\nHTTP_STATUS:%{http_code}\n" -X POST "$BASE/api/projects/$Q3_ID/export" \
    -H "Authorization: Bearer $DEV_TOK"
{"error":"only admins and members can export"}
HTTP_STATUS:403
```

Both runs report `exported: 7`, and the real Airtable base confirms exactly **7**
records exist after running the export **twice** — not 14. (The per-record field
listing in this same command hit a local bash quoting issue with `!r` inside an
f-string being interpreted as bash history expansion — harmless, but it means the
detailed listing didn't print; the record count above, which is the actual proof of
idempotency, printed correctly and is confirmed independently by the Airtable
screenshot linked below.)

**Airtable base screenshot:** https://www.awesomescreenshot.com/image/63663443?key=d0c43cf92cd4b26f3b2b9b059495cb31

## 6. Part 3a/3b demo — Comments and Activity Feed

```
$ # meera (member) posts two comments
$ curl -sS -X POST "$BASE/api/tasks/$TASK_ID/comments" -H "Authorization: Bearer $MEERA_TOK" \
    -d '{"body":"first comment"}' | python3 -m json.tool
{ "comment": { "body": "first comment", "author": {"name": "Meera Iyer", ...}, "created_at": "2026-09-17T11:27:58.042429Z", ... } }

$ curl -sS -X POST "$BASE/api/tasks/$TASK_ID/comments" -H "Authorization: Bearer $MEERA_TOK" \
    -d '{"body":"second comment"}' | python3 -m json.tool
{ "comment": { "body": "second comment", "created_at": "2026-09-17T11:27:58.222813Z", ... } }

$ # comments listed chronologically
$ curl -sS "$BASE/api/tasks/$TASK_ID/comments" -H "Authorization: Bearer $MEERA_TOK" | python3 -m json.tool
{ "comments": [ {"body": "first comment", ...}, {"body": "second comment", ...} ] }

$ # dev (viewer) CAN read comments
$ curl -sS -w "\nHTTP_STATUS:%{http_code}\n" "$BASE/api/tasks/$TASK_ID/comments" \
    -H "Authorization: Bearer $DEV_TOK" -o /dev/null
HTTP_STATUS:200

$ # dev (viewer) CANNOT post a comment
$ curl -sS -w "\nHTTP_STATUS:%{http_code}\n" -X POST "$BASE/api/tasks/$TASK_ID/comments" \
    -H "Authorization: Bearer $DEV_TOK" -d '{"body":"nope"}'
{"error":"viewers cannot post comments"}
HTTP_STATUS:403

$ # activity feed, newest first - dev (viewer, still a project member) can read it
$ curl -sS "$BASE/api/projects/$Q3_ID/activity" -H "Authorization: Bearer $DEV_TOK" | ...
  task_status_changed       by Meera Iyer   {'to': 'in_progress', 'from': 'todo', 'task_title': '3a/3b demo task'}
  comment_added             by Meera Iyer   {'task_title': '3a/3b demo task', 'body_preview': 'second comment'}
  comment_added             by Meera Iyer   {'task_title': '3a/3b demo task', 'body_preview': 'first comment'}
  task_created               by Meera Iyer   {'status': 'todo', 'task_title': '3a/3b demo task'}
  task_assignee_changed      by Meera Iyer   {'to': 'e9cc17a3-...', 'from': None, 'task_title': 'Create a table'}

$ # lina (not a Q3 Launch member) blocked from both comments and activity
$ curl -sS -w "\nHTTP_STATUS:%{http_code}\n" "$BASE/api/tasks/$TASK_ID/comments" \
    -H "Authorization: Bearer $LINA_TOK" -o /dev/null
HTTP_STATUS:403
$ curl -sS -w "\nHTTP_STATUS:%{http_code}\n" "$BASE/api/projects/$Q3_ID/activity" \
    -H "Authorization: Bearer $LINA_TOK" -o /dev/null
HTTP_STATUS:403
```

Note the `task_assignee_changed` entry for "Create a table" in the activity feed —
that's from real UI usage in the browser during the session (not scripted), confirming
the feature works end-to-end through the frontend as well as the API.

## 7. Final test run

```
$ docker compose exec backend python -m pytest -v
collected 36 items

projects/tests.py::TestProjects (4) PASSED
projects/tests.py::TestTasks (6) PASSED
projects/tests.py::TestExport (6) PASSED
projects/tests.py::TestTaskPatchAccessControl (2) PASSED
projects/tests.py::TestComments (4) PASSED
projects/tests.py::TestActivity (6) PASSED
users/tests.py::TestRegister (4) PASSED
users/tests.py::TestLogin (4) PASSED

======================= 36 passed, 58 warnings in 58.49s =======================

$ docker compose exec frontend npm test
> vitest run

 ✓ src/tests/schemas.test.ts (6)
 ✓ src/tests/TaskCard.test.tsx (3)

 Test Files  2 passed (2)
      Tests  9 passed (9)
   Duration  3.64s
```

**Final: 36 backend tests passed, 9 frontend tests passed. Zero failures.**
