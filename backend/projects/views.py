from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.db import transaction
from django.db.models import Q
from users.serializers import UserSerializer
from .models import Project, Membership, Task, Comment, Activity
from .serializers import ProjectDetailSerializer, TaskSerializer, CommentSerializer, ActivitySerializer
from .airtable_client import export_tasks, AirtableConfigError


def _get_membership(user, project_id):
    try:
        return Membership.objects.get(user=user, project_id=project_id)
    except Membership.DoesNotExist:
        return None


def _can_edit_tasks(role):
    return role in ('admin', 'member')


class ProjectListCreateView(APIView):
    def get(self, request):
        memberships = (
            Membership.objects
            .filter(user=request.user)
            .select_related('project__owner')
            .prefetch_related('project__tasks')
            .order_by('-project__created_at')
        )
        projects = []
        for m in memberships:
            p = m.project
            projects.append({
                'id': str(p.id),
                'name': p.name,
                'description': p.description,
                'role': m.role,
                'owner': UserSerializer(p.owner).data,
                'taskCount': p.tasks.count(),
                'createdAt': p.created_at.isoformat(),
            })
        return Response({'projects': projects})

    def post(self, request):
        name = (request.data.get('name') or '').strip()
        description = request.data.get('description') or None
        if not name or len(name) > 120:
            return Response({'error': 'invalid input'}, status=status.HTTP_400_BAD_REQUEST)
        project = Project.objects.create(name=name, description=description, owner=request.user)
        Membership.objects.create(user=request.user, project=project, role='admin')
        return Response(
            {'project': {'id': str(project.id), 'name': project.name}},
            status=status.HTTP_201_CREATED,
        )


class ProjectDetailView(APIView):
    def get(self, request, project_id):
        membership = _get_membership(request.user, project_id)
        if not membership:
            return Response({'error': 'forbidden'}, status=status.HTTP_403_FORBIDDEN)
        try:
            project = (
                Project.objects
                .prefetch_related('memberships__user', 'tasks__assignee', 'tasks__created_by')
                .select_related('owner')
                .get(id=project_id)
            )
        except Project.DoesNotExist:
            return Response({'error': 'not found'}, status=status.HTTP_404_NOT_FOUND)
        return Response({'project': ProjectDetailSerializer(project).data})

    def patch(self, request, project_id):
        membership = _get_membership(request.user, project_id)
        if not membership:
            return Response({'error': 'forbidden'}, status=status.HTTP_403_FORBIDDEN)
        if membership.role != 'admin':
            return Response({'error': 'only admins can update projects'}, status=status.HTTP_403_FORBIDDEN)
        try:
            project = Project.objects.get(id=project_id)
        except Project.DoesNotExist:
            return Response({'error': 'not found'}, status=status.HTTP_404_NOT_FOUND)
        if 'name' in request.data:
            project.name = request.data['name'].strip()
        if 'description' in request.data:
            project.description = request.data['description'] or None
        project.save()
        return Response({'project': {'id': str(project.id), 'name': project.name}})

    def delete(self, request, project_id):
        membership = _get_membership(request.user, project_id)
        if not membership:
            return Response({'error': 'forbidden'}, status=status.HTTP_403_FORBIDDEN)
        if membership.role != 'admin':
            return Response({'error': 'only admins can delete projects'}, status=status.HTTP_403_FORBIDDEN)
        try:
            project = Project.objects.get(id=project_id)
        except Project.DoesNotExist:
            return Response({'error': 'not found'}, status=status.HTTP_404_NOT_FOUND)
        project.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class TaskListCreateView(APIView):
    def get(self, request, project_id):
        membership = _get_membership(request.user, project_id)
        if not membership:
            return Response({'error': 'forbidden'}, status=status.HTTP_403_FORBIDDEN)

        tasks = Task.objects.filter(project_id=project_id).select_related('assignee')

        q = request.query_params.get('q')
        if q:
            tasks = tasks.filter(Q(title__icontains=q) | Q(description__icontains=q))

        tasks = tasks.order_by('status', 'position')
        return Response({'tasks': TaskSerializer(tasks, many=True).data})

    def post(self, request, project_id):
        membership = _get_membership(request.user, project_id)
        if not membership:
            return Response({'error': 'forbidden'}, status=status.HTTP_403_FORBIDDEN)
        if not _can_edit_tasks(membership.role):
            return Response({'error': 'viewers cannot create tasks'}, status=status.HTTP_403_FORBIDDEN)

        title = (request.data.get('title') or '').strip()
        if not title:
            return Response({'error': 'title is required'}, status=status.HTTP_400_BAD_REQUEST)

        task_status = request.data.get('status', 'todo')
        if task_status not in ('todo', 'in_progress', 'review', 'done'):
            return Response({'error': 'invalid status'}, status=status.HTTP_400_BAD_REQUEST)

        last = Task.objects.filter(project_id=project_id, status=task_status).order_by('-position').first()
        position = (last.position + 1) if last else 0

        with transaction.atomic():
            task = Task.objects.create(
                project_id=project_id,
                title=title,
                description=request.data.get('description') or None,
                status=task_status,
                assignee_id=request.data.get('assigneeId') or None,
                created_by=request.user,
                position=position,
            )
            Activity.objects.create(
                project_id=project_id,
                task=task,
                actor=request.user,
                verb='task_created',
                metadata={'task_title': task.title, 'status': task.status},
            )
        task_data = TaskSerializer(Task.objects.select_related('assignee').get(id=task.id)).data
        return Response({'task': task_data}, status=status.HTTP_201_CREATED)


class TaskDetailView(APIView):
    def patch(self, request, task_id):
        try:
            task = Task.objects.select_related('project').get(id=task_id)
        except Task.DoesNotExist:
            return Response({'error': 'not found'}, status=status.HTTP_404_NOT_FOUND)

        membership = _get_membership(request.user, task.project_id)
        if not membership:
            return Response({'error': 'forbidden'}, status=status.HTTP_403_FORBIDDEN)
        if not _can_edit_tasks(membership.role):
            return Response({'error': 'viewers cannot edit tasks'}, status=status.HTTP_403_FORBIDDEN)

        pending_activities = []

        if 'title' in request.data:
            task.title = request.data['title'].strip()
        if 'description' in request.data:
            task.description = request.data['description'] or None
        if 'status' in request.data:
            new_status = request.data['status']
            if new_status not in ('todo', 'in_progress', 'review', 'done'):
                return Response({'error': 'invalid status'}, status=status.HTTP_400_BAD_REQUEST)
            if new_status != task.status:
                pending_activities.append(('task_status_changed', {'from': task.status, 'to': new_status}))
            task.status = new_status
        if 'assigneeId' in request.data:
            new_assignee_id = request.data['assigneeId'] or None
            old_assignee_id = str(task.assignee_id) if task.assignee_id else None
            if new_assignee_id != old_assignee_id:
                pending_activities.append(('task_assignee_changed', {'from': old_assignee_id, 'to': new_assignee_id}))
            task.assignee_id = new_assignee_id

        with transaction.atomic():
            task.save()
            for verb, metadata in pending_activities:
                Activity.objects.create(
                    project_id=task.project_id,
                    task=task,
                    actor=request.user,
                    verb=verb,
                    metadata={**metadata, 'task_title': task.title},
                )

        task_data = TaskSerializer(Task.objects.select_related('assignee').get(id=task_id)).data
        return Response({'task': task_data})

    def delete(self, request, task_id):
        try:
            task = Task.objects.select_related('project').get(id=task_id)
        except Task.DoesNotExist:
            return Response({'error': 'not found'}, status=status.HTTP_404_NOT_FOUND)

        membership = _get_membership(request.user, str(task.project_id))
        if not membership:
            return Response({'error': 'forbidden'}, status=status.HTTP_403_FORBIDDEN)
        if not _can_edit_tasks(membership.role):
            return Response({'error': 'viewers cannot delete tasks'}, status=status.HTTP_403_FORBIDDEN)

        task.delete()
        return Response({'ok': True})


class MemberAddView(APIView):
    def post(self, request, project_id):
        membership = _get_membership(request.user, project_id)
        if not membership:
            return Response({'error': 'forbidden'}, status=status.HTTP_403_FORBIDDEN)
        if membership.role != 'admin':
            return Response({'error': 'only admins can add members'}, status=status.HTTP_403_FORBIDDEN)

        email = (request.data.get('email') or '').strip()
        role = request.data.get('role', 'member')
        if role not in ('admin', 'member', 'viewer'):
            return Response({'error': 'invalid role'}, status=status.HTTP_400_BAD_REQUEST)

        from users.models import User as UserModel
        try:
            user = UserModel.objects.get(email=email)
        except UserModel.DoesNotExist:
            return Response({'error': 'user not found'}, status=status.HTTP_404_NOT_FOUND)

        membership_obj, created = Membership.objects.get_or_create(
            user=user,
            project_id=project_id,
            defaults={'role': role},
        )
        if not created:
            membership_obj.role = role
            membership_obj.save()

        return Response({'ok': True, 'role': membership_obj.role}, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class ExportView(APIView):
    def post(self, request, project_id):
        membership = _get_membership(request.user, project_id)
        if not membership:
            return Response({'error': 'forbidden'}, status=status.HTTP_403_FORBIDDEN)
        if not _can_edit_tasks(membership.role):
            return Response({'error': 'only admins and members can export'}, status=status.HTTP_403_FORBIDDEN)

        tasks = Task.objects.filter(project_id=project_id).select_related('assignee', 'project')
        try:
            result = export_tasks(tasks)
        except AirtableConfigError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response({'exported': result['exported'], 'failed': result['failed']})


class CommentListCreateView(APIView):
    """Chronological, append-only comment thread on a task (Part 3a). There is
    intentionally no PATCH/DELETE route for comments anywhere in urls.py -
    once posted, a comment cannot be edited or removed."""

    def get(self, request, task_id):
        try:
            task = Task.objects.get(id=task_id)
        except Task.DoesNotExist:
            return Response({'error': 'not found'}, status=status.HTTP_404_NOT_FOUND)

        membership = _get_membership(request.user, task.project_id)
        if not membership:
            return Response({'error': 'forbidden'}, status=status.HTTP_403_FORBIDDEN)

        comments = Comment.objects.filter(task_id=task_id).select_related('author').order_by('created_at')
        return Response({'comments': CommentSerializer(comments, many=True).data})

    def post(self, request, task_id):
        try:
            task = Task.objects.get(id=task_id)
        except Task.DoesNotExist:
            return Response({'error': 'not found'}, status=status.HTTP_404_NOT_FOUND)

        membership = _get_membership(request.user, task.project_id)
        if not membership:
            return Response({'error': 'forbidden'}, status=status.HTTP_403_FORBIDDEN)
        if not _can_edit_tasks(membership.role):
            return Response({'error': 'viewers cannot post comments'}, status=status.HTTP_403_FORBIDDEN)

        body = (request.data.get('body') or '').strip()
        if not body:
            return Response({'error': 'body is required'}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            comment = Comment.objects.create(task=task, author=request.user, body=body)
            Activity.objects.create(
                project_id=task.project_id,
                task=task,
                actor=request.user,
                verb='comment_added',
                metadata={'task_title': task.title, 'body_preview': body[:140]},
            )

        comment = Comment.objects.select_related('author').get(id=comment.id)
        return Response({'comment': CommentSerializer(comment).data}, status=status.HTTP_201_CREATED)


class ActivityListView(APIView):
    """Recent-activity feed for a project (Part 3b): members-only, newest first."""

    RECENT_LIMIT = 50

    def get(self, request, project_id):
        membership = _get_membership(request.user, project_id)
        if not membership:
            return Response({'error': 'forbidden'}, status=status.HTTP_403_FORBIDDEN)

        activities = (
            Activity.objects
            .filter(project_id=project_id)
            .select_related('actor', 'task')
            .order_by('-created_at')[:self.RECENT_LIMIT]
        )
        return Response({'activities': ActivitySerializer(activities, many=True).data})
