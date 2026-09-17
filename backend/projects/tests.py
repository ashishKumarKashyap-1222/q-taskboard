import pytest
from rest_framework.test import APIClient
from users.models import User
from projects.models import Project, Membership, Task, Comment
from projects import airtable_client
from projects.airtable_mock import MockAirtableTable


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture
def user(db):
    return User.objects.create_user(email='meera@taskboard.dev', name='Meera Iyer', password='password123')


@pytest.fixture
def auth_client(client, user):
    response = client.post('/api/auth/login', {
        'email': 'meera@taskboard.dev',
        'password': 'password123',
    }, format='json')
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['token']}")
    return client


@pytest.mark.django_db
class TestProjects:
    def test_create_project(self, auth_client, user):
        response = auth_client.post('/api/projects', {'name': 'My Project'}, format='json')
        assert response.status_code == 201
        assert response.data['project']['name'] == 'My Project'

    def test_list_only_returns_member_projects(self, auth_client, user):
        p1 = Project.objects.create(name='Mine', owner=user)
        Membership.objects.create(user=user, project=p1, role='admin')
        other = User.objects.create_user(email='other@example.com', name='Other', password='password123')
        p2 = Project.objects.create(name='Not Mine', owner=other)
        Membership.objects.create(user=other, project=p2, role='admin')

        response = auth_client.get('/api/projects')
        assert response.status_code == 200
        names = [p['name'] for p in response.data['projects']]
        assert 'Mine' in names
        assert 'Not Mine' not in names

    def test_get_project_detail(self, auth_client, user):
        project = Project.objects.create(name='My Project', owner=user)
        Membership.objects.create(user=user, project=project, role='admin')

        response = auth_client.get(f'/api/projects/{project.id}')
        assert response.status_code == 200
        assert response.data['project']['name'] == 'My Project'

    def test_non_member_cannot_view_project(self, client, user):
        owner = User.objects.create_user(email='owner@example.com', name='Owner', password='password123')
        project = Project.objects.create(name='Private', owner=owner)
        Membership.objects.create(user=owner, project=project, role='admin')

        resp = client.post('/api/auth/login', {'email': 'meera@taskboard.dev', 'password': 'password123'}, format='json')
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {resp.data['token']}")

        response = client.get(f'/api/projects/{project.id}')
        assert response.status_code == 403


@pytest.mark.django_db
class TestTasks:
    def test_create_task(self, auth_client, user):
        project = Project.objects.create(name='P', owner=user)
        Membership.objects.create(user=user, project=project, role='admin')

        response = auth_client.post(f'/api/projects/{project.id}/tasks', {'title': 'Do a thing'}, format='json')
        assert response.status_code == 201
        assert response.data['task']['title'] == 'Do a thing'

    def test_viewers_cannot_create_tasks(self, client, user):
        owner = User.objects.create_user(email='owner@example.com', name='Owner', password='password123')
        project = Project.objects.create(name='P', owner=owner)
        Membership.objects.create(user=owner, project=project, role='admin')
        Membership.objects.create(user=user, project=project, role='viewer')

        resp = client.post('/api/auth/login', {'email': 'meera@taskboard.dev', 'password': 'password123'}, format='json')
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {resp.data['token']}")

        response = client.post(f'/api/projects/{project.id}/tasks', {'title': 'A task'}, format='json')
        assert response.status_code == 403

    def test_delete_task_requires_membership(self, client, user):
        owner = User.objects.create_user(email='owner@example.com', name='Owner', password='password123')
        project = Project.objects.create(name='P', owner=owner)
        Membership.objects.create(user=owner, project=project, role='admin')
        task = Task.objects.create(project=project, title='A task', created_by=owner)

        resp = client.post('/api/auth/login', {'email': 'meera@taskboard.dev', 'password': 'password123'}, format='json')
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {resp.data['token']}")

        response = client.delete(f'/api/tasks/{task.id}')
        assert response.status_code == 403

    def test_search_matches_title_or_description(self, auth_client, user):
        project = Project.objects.create(name='P', owner=user)
        Membership.objects.create(user=user, project=project, role='admin')
        Task.objects.create(project=project, title='Finalize launch date', created_by=user)
        Task.objects.create(project=project, title='Unrelated task', description='mentions launch here', created_by=user)
        Task.objects.create(project=project, title='Something else entirely', created_by=user)

        response = auth_client.get(f'/api/projects/{project.id}/tasks?q=launch')
        assert response.status_code == 200
        titles = {t['title'] for t in response.data['tasks']}
        assert titles == {'Finalize launch date', 'Unrelated task'}

    def test_search_does_not_leak_tasks_from_other_projects(self, auth_client, user):
        my_project = Project.objects.create(name='Mine', owner=user)
        Membership.objects.create(user=user, project=my_project, role='admin')
        Task.objects.create(project=my_project, title='My only task', created_by=user)

        other_owner = User.objects.create_user(email='other-owner@example.com', name='Other', password='password123')
        other_project = Project.objects.create(name='Not Mine', owner=other_owner)
        Membership.objects.create(user=other_owner, project=other_project, role='admin')
        Task.objects.create(project=other_project, title='Secret task from another project', created_by=other_owner)

        # regression test for the SQL injection fixed in views.py: a query designed to break
        # out of the old raw-SQL WHERE clause and match every row must NOT leak other projects'
        # tasks, and must not raise a server error either.
        payload = "nonexistent%') OR 1=1 -- "
        response = auth_client.get(f'/api/projects/{my_project.id}/tasks', {'q': payload})
        assert response.status_code == 200
        titles = {t['title'] for t in response.data['tasks']}
        assert titles == set()
        assert 'Secret task from another project' not in titles
        assert 'My only task' not in titles  # payload matches nothing literally, by design

    def test_search_handles_sql_special_characters_safely(self, auth_client, user):
        project = Project.objects.create(name='P', owner=user)
        Membership.objects.create(user=user, project=project, role='admin')
        Task.objects.create(project=project, title="Fix O'Brien's report", created_by=user)

        response = auth_client.get(f"/api/projects/{project.id}/tasks", {'q': "O'Brien"})
        assert response.status_code == 200
        assert response.data['tasks'][0]['title'] == "Fix O'Brien's report"


def _use_mock_table(monkeypatch, mock_table):
    """Point ExportView at a fake Airtable table instead of the real API."""
    monkeypatch.setattr(
        'projects.views.export_tasks',
        lambda tasks: airtable_client.export_tasks(tasks, table=mock_table),
    )


@pytest.mark.django_db
class TestExport:
    def test_export_requires_membership(self, client, user):
        owner = User.objects.create_user(email='export-owner1@example.com', name='Owner', password='password123')
        project = Project.objects.create(name='P', owner=owner)
        Membership.objects.create(user=owner, project=project, role='admin')

        resp = client.post('/api/auth/login', {'email': 'meera@taskboard.dev', 'password': 'password123'}, format='json')
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {resp.data['token']}")

        response = client.post(f'/api/projects/{project.id}/export')
        assert response.status_code == 403

    def test_viewers_cannot_export(self, client, user):
        owner = User.objects.create_user(email='export-owner2@example.com', name='Owner', password='password123')
        project = Project.objects.create(name='P', owner=owner)
        Membership.objects.create(user=owner, project=project, role='admin')
        Membership.objects.create(user=user, project=project, role='viewer')

        resp = client.post('/api/auth/login', {'email': 'meera@taskboard.dev', 'password': 'password123'}, format='json')
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {resp.data['token']}")

        response = client.post(f'/api/projects/{project.id}/export')
        assert response.status_code == 403

    def test_members_can_export(self, client, user, monkeypatch):
        owner = User.objects.create_user(email='export-owner3@example.com', name='Owner', password='password123')
        project = Project.objects.create(name='P', owner=owner)
        Membership.objects.create(user=owner, project=project, role='admin')
        Membership.objects.create(user=user, project=project, role='member')
        Task.objects.create(project=project, title='T', created_by=owner)

        # never hit the real Airtable API from tests - this test only cares that
        # a member gets past the permission check (not blocked at 403).
        _use_mock_table(monkeypatch, MockAirtableTable())

        resp = client.post('/api/auth/login', {'email': 'meera@taskboard.dev', 'password': 'password123'}, format='json')
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {resp.data['token']}")

        response = client.post(f'/api/projects/{project.id}/export')
        assert response.status_code != 403

    def test_export_pushes_tasks_and_reruns_are_idempotent(self, auth_client, user, monkeypatch):
        project = Project.objects.create(name='Export Proj', owner=user)
        Membership.objects.create(user=user, project=project, role='admin')
        Task.objects.create(project=project, title='Task One', created_by=user)
        Task.objects.create(project=project, title='Task Two', created_by=user)

        mock_table = MockAirtableTable()
        _use_mock_table(monkeypatch, mock_table)

        response = auth_client.post(f'/api/projects/{project.id}/export')
        assert response.status_code == 200
        assert response.data['exported'] == 2
        assert response.data['failed'] == []
        assert len(mock_table.records_by_key) == 2

        # re-running the export must update the same two rows, not duplicate them
        response2 = auth_client.post(f'/api/projects/{project.id}/export')
        assert response2.status_code == 200
        assert response2.data['exported'] == 2
        assert len(mock_table.records_by_key) == 2

    def test_export_skips_bad_record_without_failing_the_batch(self, auth_client, user, monkeypatch):
        project = Project.objects.create(name='Export Proj 2', owner=user)
        Membership.objects.create(user=user, project=project, role='admin')
        good = Task.objects.create(project=project, title='Good task', created_by=user)
        bad = Task.objects.create(project=project, title='Bad task', created_by=user)

        mock_table = MockAirtableTable(fail_task_ids={str(bad.id)})
        _use_mock_table(monkeypatch, mock_table)

        response = auth_client.post(f'/api/projects/{project.id}/export')
        assert response.status_code == 200
        assert response.data['exported'] == 1
        assert len(response.data['failed']) == 1
        assert response.data['failed'][0]['task_id'] == str(bad.id)
        assert str(good.id) in mock_table.records_by_key
        assert str(bad.id) not in mock_table.records_by_key

    def test_export_retries_transient_failure_then_succeeds(self, auth_client, user, monkeypatch):
        project = Project.objects.create(name='Export Proj 3', owner=user)
        Membership.objects.create(user=user, project=project, role='admin')
        task = Task.objects.create(project=project, title='Task', created_by=user)

        monkeypatch.setattr(airtable_client.time, 'sleep', lambda seconds: None)
        mock_table = MockAirtableTable(transient_failures=2)  # fails twice, succeeds on the 3rd (final allowed) attempt
        _use_mock_table(monkeypatch, mock_table)

        response = auth_client.post(f'/api/projects/{project.id}/export')
        assert response.status_code == 200
        assert response.data['exported'] == 1
        assert response.data['failed'] == []
        assert str(task.id) in mock_table.records_by_key


@pytest.mark.django_db
class TestTaskPatchAccessControl:
    """Regression tests for REVIEW.md issue #2: TaskDetailView.patch used to skip
    the membership check entirely. Fixed as part of instrumenting PATCH for the
    Part 3b activity feed, since correctly attributing 'who changed what' requires
    resolving project membership anyway."""

    def test_patch_requires_membership(self, client, user):
        owner = User.objects.create_user(email='patch-owner1@example.com', name='Owner', password='password123')
        project = Project.objects.create(name='P', owner=owner)
        Membership.objects.create(user=owner, project=project, role='admin')
        task = Task.objects.create(project=project, title='Original title', created_by=owner)

        resp = client.post('/api/auth/login', {'email': 'meera@taskboard.dev', 'password': 'password123'}, format='json')
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {resp.data['token']}")

        response = client.patch(f'/api/tasks/{task.id}', {'title': 'PWNED'}, format='json')
        assert response.status_code == 403
        task.refresh_from_db()
        assert task.title == 'Original title'

    def test_viewers_cannot_patch_tasks(self, client, user):
        owner = User.objects.create_user(email='patch-owner2@example.com', name='Owner', password='password123')
        project = Project.objects.create(name='P', owner=owner)
        Membership.objects.create(user=owner, project=project, role='admin')
        Membership.objects.create(user=user, project=project, role='viewer')
        task = Task.objects.create(project=project, title='Original title', created_by=owner)

        resp = client.post('/api/auth/login', {'email': 'meera@taskboard.dev', 'password': 'password123'}, format='json')
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {resp.data['token']}")

        response = client.patch(f'/api/tasks/{task.id}', {'title': 'nope'}, format='json')
        assert response.status_code == 403


@pytest.mark.django_db
class TestComments:
    def test_comments_listed_chronologically_with_author_and_body(self, auth_client, user):
        project = Project.objects.create(name='P', owner=user)
        Membership.objects.create(user=user, project=project, role='admin')
        task = Task.objects.create(project=project, title='T', created_by=user)

        auth_client.post(f'/api/tasks/{task.id}/comments', {'body': 'first comment'}, format='json')
        auth_client.post(f'/api/tasks/{task.id}/comments', {'body': 'second comment'}, format='json')

        response = auth_client.get(f'/api/tasks/{task.id}/comments')
        assert response.status_code == 200
        bodies = [c['body'] for c in response.data['comments']]
        assert bodies == ['first comment', 'second comment']
        first = response.data['comments'][0]
        assert first['author']['email'] == 'meera@taskboard.dev'
        assert 'created_at' in first

    def test_viewer_can_read_but_not_post(self, client, user):
        owner = User.objects.create_user(email='comment-owner1@example.com', name='Owner', password='password123')
        project = Project.objects.create(name='P', owner=owner)
        Membership.objects.create(user=owner, project=project, role='admin')
        Membership.objects.create(user=user, project=project, role='viewer')
        task = Task.objects.create(project=project, title='T', created_by=owner)
        Comment.objects.create(task=task, author=owner, body='existing comment')

        resp = client.post('/api/auth/login', {'email': 'meera@taskboard.dev', 'password': 'password123'}, format='json')
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {resp.data['token']}")

        read_response = client.get(f'/api/tasks/{task.id}/comments')
        assert read_response.status_code == 200
        assert len(read_response.data['comments']) == 1

        post_response = client.post(f'/api/tasks/{task.id}/comments', {'body': 'trying to post'}, format='json')
        assert post_response.status_code == 403

    def test_non_member_cannot_read_or_post(self, client, user):
        owner = User.objects.create_user(email='comment-owner2@example.com', name='Owner', password='password123')
        project = Project.objects.create(name='P', owner=owner)
        Membership.objects.create(user=owner, project=project, role='admin')
        task = Task.objects.create(project=project, title='T', created_by=owner)

        resp = client.post('/api/auth/login', {'email': 'meera@taskboard.dev', 'password': 'password123'}, format='json')
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {resp.data['token']}")

        assert client.get(f'/api/tasks/{task.id}/comments').status_code == 403
        assert client.post(f'/api/tasks/{task.id}/comments', {'body': 'x'}, format='json').status_code == 403

    def test_comments_have_no_edit_or_delete_route(self, auth_client, user):
        project = Project.objects.create(name='P', owner=user)
        Membership.objects.create(user=user, project=project, role='admin')
        task = Task.objects.create(project=project, title='T', created_by=user)
        auth_client.post(f'/api/tasks/{task.id}/comments', {'body': 'immutable'}, format='json')

        # there is no per-comment URL at all - PATCH/DELETE on the collection
        # endpoint itself is simply not a route DRF recognizes.
        assert auth_client.patch(f'/api/tasks/{task.id}/comments', {'body': 'edited'}, format='json').status_code == 405
        assert auth_client.delete(f'/api/tasks/{task.id}/comments').status_code == 405


@pytest.mark.django_db
class TestActivity:
    def test_task_created_writes_activity(self, auth_client, user):
        project = Project.objects.create(name='P', owner=user)
        Membership.objects.create(user=user, project=project, role='admin')

        auth_client.post(f'/api/projects/{project.id}/tasks', {'title': 'New task'}, format='json')

        response = auth_client.get(f'/api/projects/{project.id}/activity')
        assert response.status_code == 200
        verbs = [a['verb'] for a in response.data['activities']]
        assert 'task_created' in verbs

    def test_status_and_assignee_changes_write_activity(self, auth_client, user):
        project = Project.objects.create(name='P', owner=user)
        Membership.objects.create(user=user, project=project, role='admin')
        other = User.objects.create_user(email='assignee@example.com', name='Assignee', password='password123')
        Membership.objects.create(user=other, project=project, role='member')
        task = Task.objects.create(project=project, title='T', status='todo', created_by=user)

        auth_client.patch(f'/api/tasks/{task.id}', {'status': 'in_progress', 'assigneeId': str(other.id)}, format='json')

        response = auth_client.get(f'/api/projects/{project.id}/activity')
        by_verb = {a['verb']: a for a in response.data['activities']}
        assert by_verb['task_status_changed']['metadata'] == {'from': 'todo', 'to': 'in_progress', 'task_title': 'T'}
        assert by_verb['task_assignee_changed']['metadata']['to'] == str(other.id)

    def test_no_op_patch_does_not_create_spurious_activity(self, auth_client, user):
        project = Project.objects.create(name='P', owner=user)
        Membership.objects.create(user=user, project=project, role='admin')
        task = Task.objects.create(project=project, title='T', status='todo', created_by=user)

        auth_client.patch(f'/api/tasks/{task.id}', {'status': 'todo', 'title': 'T'}, format='json')

        response = auth_client.get(f'/api/projects/{project.id}/activity')
        assert response.data['activities'] == []

    def test_comment_added_writes_activity(self, auth_client, user):
        project = Project.objects.create(name='P', owner=user)
        Membership.objects.create(user=user, project=project, role='admin')
        task = Task.objects.create(project=project, title='T', created_by=user)

        auth_client.post(f'/api/tasks/{task.id}/comments', {'body': 'hello'}, format='json')

        response = auth_client.get(f'/api/projects/{project.id}/activity')
        verbs = [a['verb'] for a in response.data['activities']]
        assert 'comment_added' in verbs

    def test_activity_ordered_most_recent_first(self, auth_client, user):
        project = Project.objects.create(name='P', owner=user)
        Membership.objects.create(user=user, project=project, role='admin')

        auth_client.post(f'/api/projects/{project.id}/tasks', {'title': 'First task'}, format='json')
        auth_client.post(f'/api/projects/{project.id}/tasks', {'title': 'Second task'}, format='json')

        response = auth_client.get(f'/api/projects/{project.id}/activity')
        titles = [a['metadata']['task_title'] for a in response.data['activities']]
        assert titles == ['Second task', 'First task']

    def test_activity_requires_membership(self, client, user):
        owner = User.objects.create_user(email='activity-owner@example.com', name='Owner', password='password123')
        project = Project.objects.create(name='P', owner=owner)
        Membership.objects.create(user=owner, project=project, role='admin')

        resp = client.post('/api/auth/login', {'email': 'meera@taskboard.dev', 'password': 'password123'}, format='json')
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {resp.data['token']}")

        response = client.get(f'/api/projects/{project.id}/activity')
        assert response.status_code == 403
