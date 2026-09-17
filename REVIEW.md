# Code Review — TaskBoard

Top 4 issues, ranked by business impact. All file/line references are against the
codebase as delivered.

---

## 1. SQL Injection in task search

- **File / line:** `backend/projects/views.py:110-123` (`TaskListCreateView.get`)
- **Category:** Security
- **Severity:** Critical

The task search branch builds a raw SQL statement with an f-string and interpolates
`project_id` (a URL path param) and `q` (a user-supplied query param) directly into the
query text, instead of using parameterized `cursor.execute(sql, params)` or the Django
ORM:

```python
sql = (
    f"SELECT id, project_id, title, description, status, assignee_id, created_by_id, position, created_at, updated_at "
    f"FROM tasks "
    f"WHERE project_id = '{project_id}' "
    f"AND (title ILIKE '%{q}%' OR description ILIKE '%{q}%') "
    f"ORDER BY position ASC"
)
cursor.execute(sql)
```

Any project member (this endpoint requires an existing membership, but no more than
that) can pass a crafted `q` to break out of the string literal — reading unrelated
tables (e.g. `users`, including password hashes) via `UNION SELECT`, or running
destructive statements, since psycopg2 will happily execute a semicolon-separated
multi-statement string handed to it this way. This is the single highest-impact issue in
the codebase: a member of any one project can potentially compromise the entire
database, not just their own project's data.

**Recommended fix:** parameterize the query (`cursor.execute(sql, [project_id, like_pattern, like_pattern])`)
or, more simply, drop the raw SQL entirely and use the ORM:
`Task.objects.filter(project_id=project_id).filter(Q(title__icontains=q) | Q(description__icontains=q)).order_by('position')`.

**→ This is the issue fixed in Part 2 (see `REVIEW.md` §Part 2 commit / `TERMINAL_LOG.md`).**

---

## 2. Broken access control on task edits

- **File / line:** `backend/projects/views.py:165-185` (`TaskDetailView.patch`)
- **Category:** Security / Data Integrity
- **Severity:** Critical

`TaskDetailView.patch` never resolves or checks project membership before applying the
update — it loads the task by ID and writes straight to it. Contrast with `.delete`
seven lines below (`views.py:187-200`), which correctly calls `_get_membership` and
`_can_edit_tasks`. The result: **any authenticated user, regardless of project
membership, can edit the title, description, status, or assignee of any task in any
project**, simply by knowing (or guessing/enumerating) its UUID.

**Proof** (fresh data created via the API for this demo — no seed data touched):

```bash
# meera creates a project + task normally, as an admin
curl -sS -X POST http://localhost:8000/api/projects \
  -H "Content-Type: application/json" -H "Authorization: Bearer $MEERA_TOK" \
  -d '{"name":"Access Control Bug Demo"}'
# → {"project":{"id":"eafb63a6-1b3f-4a82-9053-afcead3f109f", ...}}

curl -sS -X POST http://localhost:8000/api/projects/eafb63a6-1b3f-4a82-9053-afcead3f109f/tasks \
  -H "Content-Type: application/json" -H "Authorization: Bearer $MEERA_TOK" \
  -d '{"title":"Sensitive task - members only"}'
# → {"task":{"id":"05efd440-7808-4679-90cc-f04cc1868349", ...}}

# lina (logged in, but NOT a member of this project) is correctly blocked from *viewing* it:
curl -sS -w "\nHTTP_STATUS:%{http_code}\n" \
  http://localhost:8000/api/projects/eafb63a6-1b3f-4a82-9053-afcead3f109f \
  -H "Authorization: Bearer $LINA_TOK"
# → {"error":"forbidden"}
# → HTTP_STATUS:403

# THE BUG: lina can still PATCH a task inside that same project she can't even view:
curl -sS -w "\nHTTP_STATUS:%{http_code}\n" -X PATCH \
  http://localhost:8000/api/tasks/05efd440-7808-4679-90cc-f04cc1868349 \
  -H "Content-Type: application/json" -H "Authorization: Bearer $LINA_TOK" \
  -d '{"title":"PWNED by non-member lina"}'
# → {"task":{"id":"05efd440-...","project_id":"eafb63a6-...","title":"PWNED by non-member lina", ...}}
# → HTTP_STATUS:200   (expected: 403)
```

Full transcript of this run is in `TERMINAL_LOG.md`. The task's title is now genuinely
changed in the database — the response body confirms it directly.

**Recommended fix:** mirror `.delete`'s pattern — resolve the task, look up
`_get_membership(request.user, task.project_id)`, 403 if absent, and additionally gate
mutation on `_can_edit_tasks(membership.role)` so viewers can't edit either (today
`.patch` doesn't even check that a *member* isn't a mere viewer).

*(Not code-fixed as part of Part 2 — that addressed only the #1-ranked issue above, per
the assignment's "pick your highest-priority issue and fix it." It ended up fixed
anyway as a side effect of Part 3b: correctly attributing "who changed what" in the
activity feed requires resolving the task's project and the actor's membership in
`TaskDetailView.patch` regardless, so the missing check above was added at the same
time, along with the missing `_can_edit_tasks` gate for viewers — see
`backend/projects/views.py` `TaskDetailView.patch` and the
`TestTaskPatchAccessControl` tests in `backend/projects/tests.py`.)*

---

## 3. N+1 query pattern despite `prefetch_related`

- **File / line:** `backend/projects/views.py:21-42` (`ProjectListCreateView.get`)
- **Category:** Performance
- **Severity:** High

The project list view prefetches each project's tasks (`.prefetch_related('project__tasks')`)
specifically to avoid N+1 queries, but then calls `p.tasks.count()` inside the loop:

```python
.prefetch_related('project__tasks')
...
for m in memberships:
    p = m.project
    ...
    'taskCount': p.tasks.count(),
```

`.count()` on a related manager always issues a fresh `SELECT COUNT(*)` — it does not
use the prefetch cache, so the prefetch is wasted work and the view still does one query
per project (N+1) on top of the membership query. On a user with many projects, this
scales linearly with project count and gets worse as `GET /api/projects` is the first
call the dashboard makes on every page load.

**Recommended fix:** use `len(p.tasks.all())` instead of `p.tasks.count()` to read from
the already-prefetched cache, or better, use `.annotate(task_count=Count('tasks'))` on
the base queryset so the count comes back in a single query with no Python-side loop at
all.

---

## 4. Non-atomic position assignment on task creation

- **File / line:** `backend/projects/views.py:148-149` (`TaskListCreateView.post`)
- **Category:** Data Integrity
- **Severity:** Medium

The next task's `position` within a status column is computed by reading the current
max and adding one, with no locking or uniqueness constraint:

```python
last = Task.objects.filter(project_id=project_id, status=task_status).order_by('-position').first()
position = (last.position + 1) if last else 0
```

Two concurrent `POST` requests to the same project/status (e.g. two team members adding
a card to "To Do" at the same moment) can both read the same `last.position` before
either writes, producing two tasks with the same `position`. There's no DB-level
uniqueness constraint on `(project, status, position)` to catch this, so the Kanban
column ordering silently becomes ambiguous/unstable — a real risk given this is a
multi-user collaborative board by design.

**Recommended fix:** wrap the read-then-write in `transaction.atomic()` with
`select_for_update()` on the filtered queryset, or switch to a DB-generated sequence
(e.g. a `(project_id, status)`-scoped counter, or fractional/lexicographic positions
that don't require reading current state at all).
