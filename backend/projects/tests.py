import pytest
from rest_framework.test import APIClient
from users.models import User
from projects.models import Project, Membership, Task


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
