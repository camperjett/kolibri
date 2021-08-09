import logging
import ntpath
import os
import shutil
from functools import partial
from tempfile import mkstemp

import requests
from django.apps.registry import AppRegistryNotReady
from django.core.files.uploadedfile import InMemoryUploadedFile
from django.core.files.uploadedfile import UploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.http.response import Http404
from django.http.response import HttpResponseBadRequest
from django.utils.translation import get_language_from_request
from django.utils.translation import gettext_lazy as _
from morango.models import ScopeDefinition
from morango.sync.controller import MorangoProfileController
from requests.exceptions import HTTPError
from rest_framework import decorators
from rest_framework import serializers
from rest_framework import status
from rest_framework import viewsets
from rest_framework.authentication import BasicAuthentication
from rest_framework.authentication import SessionAuthentication
from rest_framework.exceptions import APIException
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.exceptions import ParseError
from rest_framework.exceptions import PermissionDenied
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from six import string_types

from .permissions import FacilitySyncPermissions
from kolibri.core.auth.constants.morango_sync import PROFILE_FACILITY_DATA
from kolibri.core.auth.constants.morango_sync import State as FacilitySyncState
from kolibri.core.auth.management.utils import get_client_and_server_certs
from kolibri.core.auth.management.utils import get_dataset_id
from kolibri.core.auth.models import Facility
from kolibri.core.content.permissions import CanExportLogs
from kolibri.core.content.permissions import CanImportUsers
from kolibri.core.content.permissions import CanManageContent
from kolibri.core.content.utils.channels import read_channel_metadata_from_db_file
from kolibri.core.content.utils.paths import get_channel_lookup_url
from kolibri.core.content.utils.paths import get_content_database_file_path
from kolibri.core.content.utils.upgrade import diff_stats
from kolibri.core.device.permissions import IsSuperuser
from kolibri.core.device.permissions import NotProvisionedCanPost
from kolibri.core.discovery.utils.network.client import NetworkClient
from kolibri.core.discovery.utils.network.errors import NetworkLocationNotFound
from kolibri.core.discovery.utils.network.errors import URLParseError
from kolibri.core.tasks.exceptions import JobNotFound
from kolibri.core.tasks.exceptions import JobNotRestartable
from kolibri.core.tasks.job import JobRegistry
from kolibri.core.tasks.job import State
from kolibri.core.tasks.main import facility_queue
from kolibri.core.tasks.main import job_storage
from kolibri.core.tasks.main import priority_queue
from kolibri.core.tasks.main import queue
from kolibri.utils import conf

try:
    from django.apps import apps

    apps.check_apps_ready()
except AppRegistryNotReady:
    import django

    django.setup()

logger = logging.getLogger(__name__)


NETWORK_ERROR_STRING = _("There was a network error.")

DISK_IO_ERROR_STRING = _("There was a disk access error.")

CATCHALL_SERVER_ERROR_STRING = _("There was an unknown error.")


class BaseViewSet(viewsets.ViewSet):
    queues = []
    permission_classes = []

    # Adding auth classes explicitly until we find a fix for BasicAuth not
    # working on tasks API (in dev settings)
    authentication_classes = [SessionAuthentication, BasicAuthentication]

    def initial(self, request, *args, **kwargs):
        if len(self.permission_classes) == 0:
            self.permission_classes = self.default_permission_classes()
        if self.permission_classes is None:
            self.permission_classes = []
        return super(BaseViewSet, self).initial(request, *args, **kwargs)

    def default_permission_classes(self):
        # For all /api/tasks/tasks/ endpoints
        return [CanManageContent]

    def validate_create_req_data(self, request):
        """
        Validates the request data received on POST /api/tasks/tasks/.

        If `request.user` is authorized to initiate the `task` function, this returns
        a list of `request.data` otherwise raises PermissionDenied.
        """
        if isinstance(request.data, list):
            request_data_list = request.data
        else:
            request_data_list = [request.data]

        for request_data in request_data_list:
            if "task" not in request_data:
                raise serializers.ValidationError("The 'task' field is required.")
            if not isinstance(request_data["task"], string_types):
                raise serializers.ValidationError("The 'task' value must be a string.")

            funcstr = request_data.get("task")

            # Make sure the task is registered
            try:
                registered_job = JobRegistry.REGISTERED_JOBS[funcstr]
            except KeyError:
                raise serializers.ValidationError(
                    "'{funcstr}' is not registered.".format(funcstr=funcstr)
                )

            # Check permissions the DRF way
            for permission in registered_job.permissions:
                if not permission.has_permission(request, self):
                    self.permission_denied(request)

        return request_data_list

    def list(self, request):
        jobs_response = [
            _job_to_response(j) for _queue in self.queues for j in _queue.jobs
        ]

        return Response(jobs_response)

    def create(self, request):
        """
        Enqueues a registered task for async processing.

        If the registered task has a validator then that validator is run with the
        `request` object as its argument. The dict returned by the validator is passed
        as keyword arguments to the task function.

        If the registered task has no validator then `request.data` is passed as keyword
        arguments to the task function.

        API endpoint:
            POST /api/tasks/tasks/

        Request payload parameters:
            - `task` (required): a string representing the dotted path to task function.
            - key value pairs for `task`.

        Keep in mind:
            In the validator's returning dict we can add `extra_metadata` as a key value pair
            to set `extra_metadata` for the task.
        """
        request_data_list = self.validate_create_req_data(request)

        enqueued_jobs_response = []

        # Once we have validated all the tasks, we are good to go!
        for request_data in request_data_list:

            funcstr = request_data.pop("task")
            registered_job = JobRegistry.REGISTERED_JOBS[funcstr]

            # Run validator with `request` as its argument
            if registered_job.validator is not None:
                try:
                    validator_result = registered_job.validator(request)
                except Exception as e:
                    raise e

                if not isinstance(validator_result, dict):
                    raise serializers.ValidationError("Validator must return a dict.")

                extra_metadata = validator_result.get("extra_metadata")
                if extra_metadata is not None and not isinstance(extra_metadata, dict):
                    raise serializers.ValidationError(
                        "In the dict returned by validator, 'extra_metadata' must be a dict."
                    )

                request_data = validator_result

            job_id = registered_job.enqueue(**request_data)
            enqueued_jobs_response.append(_job_to_response(job_storage.get_job(job_id)))

        if len(enqueued_jobs_response) == 1:
            enqueued_jobs_response = enqueued_jobs_response[0]

        return Response(enqueued_jobs_response)

    def retrieve(self, request, pk=None):
        for _queue in self.queues:
            try:
                task = _job_to_response(_queue.fetch_job(pk))
                break
            except JobNotFound:
                continue
        else:
            raise Http404("Task with {pk} not found".format(pk=pk))

        return Response(task)

    @decorators.action(methods=["post"], detail=False)
    def restarttask(self, request):
        """
        Restart a task with its task id given in the task_id parameter.
        """

        if "task_id" not in request.data:
            raise serializers.ValidationError("The 'task_id' field is required.")
        if not isinstance(request.data["task_id"], string_types):
            raise serializers.ValidationError("The 'task_id' should be a string.")

        resp = {}
        for _queue in self.queues:
            try:
                task_id = _queue.restart_job(request.data["task_id"])
                resp = _job_to_response(_queue.fetch_job(task_id))
                break
            except JobNotFound:
                continue
            except JobNotRestartable as e:
                raise serializers.ValidationError(str(e))

        return Response(resp)

    def destroy(self, request, pk=None):
        # unimplemented for now.
        pass

    @decorators.action(methods=["post"], detail=False)
    def canceltask(self, request):
        """
        Cancel a task with its task id given in the task_id parameter.
        """

        if "task_id" not in request.data:
            raise serializers.ValidationError("The 'task_id' field is required.")
        if not isinstance(request.data["task_id"], string_types):
            raise serializers.ValidationError("The 'task_id' should be a string.")

        for _queue in self.queues:
            try:
                _queue.cancel(request.data["task_id"])
                break
            except JobNotFound:
                continue

        return Response({})

    @decorators.action(methods=["post"], detail=False)
    def cleartasks(self, request):
        """
        Cancels all running tasks.
        """

        for _queue in self.queues:
            _queue.empty()

        return Response({})

    @decorators.action(methods=["post"], detail=False)
    def cleartask(self, request):
        # Given a single task ID, clear it from the queue
        task_id = request.data.get("task_id")
        if not task_id:
            return Response({})

        for _queue in self.queues:
            _queue.clear_job(task_id)

        return Response({"task_id": task_id})

    @decorators.action(methods=["post"], detail=False)
    def deletefinishedtasks(self, request):
        """
        Delete all tasks that have succeeded, failed, or been cancelled.
        """
        task_id = request.data.get("task_id")
        if task_id:
            for _queue in self.queues:
                _queue.clear_job(task_id)
        else:
            for _queue in self.queues:
                _queue.clear()
        return Response({})


class TasksViewSet(BaseViewSet):
    @property
    def queues(self):
        return [queue, priority_queue]

    def default_permission_classes(self):
        if self.action in ["list", "deletefinishedtasks"]:
            return [CanManageContent | CanExportLogs]
        elif self.action == "startexportlogcsv":
            return [CanExportLogs]
        elif self.action in ["importusersfromcsv", "exportuserstocsv"]:
            return [CanImportUsers]

        # For all other tasks
        return [CanManageContent]

    @decorators.action(methods=["post"], detail=False)
    def startremotebulkimport(self, request):
        if not isinstance(request.data, list):
            raise serializers.ValidationError(
                "POST data must be a list of task descriptions"
            )

        tasks = map(partial(validate_remote_import_task, request), request.data)

        job_ids = []

        for task in tasks:
            task.update({"type": "REMOTEIMPORT", "database_ready": False})
            import_job_id = queue.enqueue(
                _remoteimport,
                task["channel_id"],
                task["baseurl"],
                peer_id=task["peer_id"],
                extra_metadata=task,
                cancellable=True,
                track_progress=True,
            )
            job_ids.append(import_job_id)

        resp = [_job_to_response(queue.fetch_job(job_id)) for job_id in job_ids]

        return Response(resp)

    @decorators.action(methods=["post"], detail=False)
    def startdiskbulkimport(self, request):
        if not isinstance(request.data, list):
            raise serializers.ValidationError(
                "POST data must be a list of task descriptions"
            )

        tasks = map(partial(validate_local_import_task, request), request.data)

        job_ids = []

        for task in tasks:
            task.update({"type": "DISKIMPORT", "database_ready": False})
            import_job_id = queue.enqueue(
                _diskimport,
                task["channel_id"],
                task["datafolder"],
                drive_id=task["drive_id"],
                extra_metadata=task,
                track_progress=True,
                cancellable=True,
            )
            job_ids.append(import_job_id)

        resp = [_job_to_response(queue.fetch_job(job_id)) for job_id in job_ids]

        return Response(resp)

    @decorators.action(methods=["post"], detail=False)
    def startdiskchannelimport(self, request):
        task = validate_local_import_task(request, request.data)

        task.update({"type": "DISKCHANNELIMPORT"})

        job_id = priority_queue.enqueue(
            call_command,
            "importchannel",
            "disk",
            task["channel_id"],
            task["datafolder"],
            drive_id=task["drive_id"],
            extra_metadata=task,
            cancellable=True,
        )

        resp = _job_to_response(priority_queue.fetch_job(job_id))
        return Response(resp)

    @decorators.action(methods=["post"], detail=False)
    def startdiskcontentimport(self, request):
        task = validate_local_import_task(request, request.data)

        task.update({"type": "DISKCONTENTIMPORT"})

        job_id = queue.enqueue(
            call_command,
            "importcontent",
            "disk",
            task["channel_id"],
            task["datafolder"],
            drive_id=task["drive_id"],
            node_ids=task["node_ids"],
            exclude_node_ids=task["exclude_node_ids"],
            extra_metadata=task,
            track_progress=True,
            cancellable=True,
        )

        resp = _job_to_response(queue.fetch_job(job_id))

        return Response(resp)

    @decorators.action(methods=["post"], detail=False)
    def startbulkdelete(self, request):
        if not isinstance(request.data, list):
            raise serializers.ValidationError(
                "POST data must be a list of task descriptions"
            )

        tasks = map(partial(validate_deletion_task, request), request.data)

        job_ids = []

        for task in tasks:
            task.update({"type": "DELETECHANNEL"})
            if task["node_ids"] or task["exclude_node_ids"]:
                task["file_size"] = None
                task["total_resources"] = None
            delete_job_id = queue.enqueue(
                call_command,
                "deletecontent",
                task["channel_id"],
                track_progress=True,
                extra_metadata=task,
            )
            job_ids.append(delete_job_id)

        resp = [_job_to_response(queue.fetch_job(job_id)) for job_id in job_ids]

        return Response(resp)

    @decorators.action(methods=["post"], detail=False)
    def startdeletechannel(self, request):
        """
        Delete a channel and all its associated content from the server
        """
        task = validate_deletion_task(request, request.data)

        task.update({"type": "DELETECONTENT"})

        if task["node_ids"] or task["exclude_node_ids"]:
            task["file_size"] = None
            task["total_resources"] = None

        task_id = queue.enqueue(
            call_command,
            "deletecontent",
            task["channel_id"],
            node_ids=task["node_ids"],
            exclude_node_ids=task["exclude_node_ids"],
            force_delete=task["force_delete"],
            track_progress=True,
            extra_metadata=task,
        )

        # attempt to get the created Task, otherwise return pending status
        resp = _job_to_response(queue.fetch_job(task_id))

        return Response(resp)

    @decorators.action(methods=["post"], detail=False)
    def startdiskbulkexport(self, request):
        if not isinstance(request.data, list):
            raise serializers.ValidationError(
                "POST data must be a list of task descriptions"
            )

        tasks = map(partial(validate_local_export_task, request), request.data)

        job_ids = []

        for task in tasks:
            task.update({"type": "DISKEXPORT"})
            export_job_id = queue.enqueue(
                _localexport,
                task["channel_id"],
                task["drive_id"],
                track_progress=True,
                cancellable=True,
                extra_metadata=task,
            )
            job_ids.append(export_job_id)

        resp = [_job_to_response(queue.fetch_job(job_id)) for job_id in job_ids]

        return Response(resp)

    @decorators.action(methods=["get"], detail=False)
    def localdrive(self, request):
        drives = get_mounted_drives_with_channel_info()

        # make sure everything is a dict, before converting to JSON
        assert isinstance(drives, dict)
        out = [mountdata._asdict() for mountdata in drives.values()]

        return Response(out)

    @decorators.action(methods=["post"], detail=False)
    def importusersfromcsv(self, request):
        """
        Import users, classes, roles and roles assignemnts from a csv file.
        :param: FILE: file dictionary with the file object
        :param: csvfile: filename of the file stored in kolibri temp folder
        :param: dryrun: validate the data but don't modify the database
        :param: delete: Users not in the csv will be deleted from the facility, and classes cleared
        :returns: An object with the job information
        """

        def manage_fileobject(request, temp_dir):
            upload = UploadedFile(request.FILES["csvfile"])
            # Django uses InMemoryUploadedFile for files less than 2.5Mb
            # and TemporaryUploadedFile for bigger files:
            if type(upload.file) == InMemoryUploadedFile:
                _, filepath = mkstemp(dir=temp_dir, suffix=".upload")
                with open(filepath, "w+b") as dest:
                    filepath = dest.name
                    for chunk in upload.file.chunks():
                        dest.write(chunk)
            else:
                tmpfile = upload.file.temporary_file_path()
                filename = ntpath.basename(tmpfile)
                filepath = os.path.join(temp_dir, filename)
                shutil.copy(tmpfile, filepath)
            return filepath

        temp_dir = os.path.join(conf.KOLIBRI_HOME, "temp")
        if not os.path.isdir(temp_dir):
            os.mkdir(temp_dir)

        locale = get_language_from_request(request)
        # the request must contain either an object file
        # or the filename of the csv stored in Kolibri temp folder
        # Validation will provide the file object, while
        # Importing will provide the filename, previously validated
        if not request.FILES:
            filename = request.data.get("csvfile", None)
            if filename:
                filepath = os.path.join(temp_dir, filename)
            else:
                return HttpResponseBadRequest("The request must contain a file object")
        else:
            if "csvfile" not in request.FILES:
                return HttpResponseBadRequest("Wrong file object")
            filepath = manage_fileobject(request, temp_dir)

        delete = request.data.get("delete", None)
        dryrun = request.data.get("dryrun", None)
        userid = request.user.pk
        facility_id = request.data.get("facility_id", None)
        job_type = "IMPORTUSERSFROMCSV"
        job_metadata = {"type": job_type, "started_by": userid, "facility": facility_id}
        job_args = ["bulkimportusers"]
        if dryrun:
            job_args.append("--dryrun")
        if delete:
            job_args.append("--delete")
        job_args.append(filepath)

        job_kwd_args = {
            "facility": facility_id,
            "userid": userid,
            "locale": locale,
            "extra_metadata": job_metadata,
            "track_progress": True,
        }

        job_id = priority_queue.enqueue(call_command, *job_args, **job_kwd_args)

        resp = _job_to_response(priority_queue.fetch_job(job_id))

        return Response(resp)

    @decorators.action(methods=["post"], detail=False)
    def exportuserstocsv(self, request):
        """
        Export users, classes, roles and roles assignemnts to a csv file.

        :param: facility_id
        :returns: An object with the job information

        """
        facility_id = request.data.get("facility_id", None)

        try:
            if facility_id:
                facility = Facility.objects.get(pk=facility_id).id
            else:
                facility = request.user.facility
        except Facility.DoesNotExist:
            raise serializers.ValidationError(
                "Facility with ID {} does not exist".format(facility_id)
            )

        job_type = "EXPORTUSERSTOCSV"
        job_metadata = {
            "type": job_type,
            "started_by": request.user.pk,
            "facility": facility,
        }
        locale = get_language_from_request(request)

        job_id = priority_queue.enqueue(
            call_command,
            "bulkexportusers",
            facility=facility,
            locale=locale,
            overwrite="true",
            extra_metadata=job_metadata,
            track_progress=True,
        )

        resp = _job_to_response(priority_queue.fetch_job(job_id))

        return Response(resp)

    @decorators.action(methods=["post"], detail=False)
    def channeldiffstats(self, request):
        job_metadata = {}
        channel_id = request.data.get("channel_id")
        method = request.data.get("method")
        drive_id = request.data.get("drive_id")
        baseurl = request.data.get("baseurl")

        # request validation and job metadata info
        if not channel_id:
            raise serializers.ValidationError("The channel_id field is required.")
        if not method:
            raise serializers.ValidationError("The method field is required.")

        if method == "network":
            baseurl = baseurl or conf.OPTIONS["Urls"]["CENTRAL_CONTENT_BASE_URL"]
            job_metadata["baseurl"] = baseurl
            # get channel version metadata
            url = get_channel_lookup_url(baseurl=baseurl, identifier=channel_id)
            resp = requests.get(url)
            channel_metadata = resp.json()
            job_metadata["new_channel_version"] = channel_metadata[0]["version"]
        elif method == "disk":
            if not drive_id:
                raise serializers.ValidationError(
                    "The drive_id field is required when using 'disk' method."
                )
            job_metadata = _add_drive_info(job_metadata, request.data)
            # get channel version metadata
            drive = get_mounted_drive_by_id(drive_id)
            channel_metadata = read_channel_metadata_from_db_file(
                get_content_database_file_path(channel_id, drive.datafolder)
            )
            job_metadata["new_channel_version"] = channel_metadata.version
        else:
            raise serializers.ValidationError(
                "'method' field should either be 'network' or 'disk'."
            )

        job_metadata.update(
            {
                "type": "CHANNELDIFFSTATS",
                "started_by": request.user.pk,
                "channel_id": channel_id,
            }
        )

        job_id = priority_queue.enqueue(
            diff_stats,
            channel_id,
            method,
            drive_id=drive_id,
            baseurl=baseurl,
            extra_metadata=job_metadata,
            track_progress=False,
            cancellable=True,
        )

        resp = _job_to_response(priority_queue.fetch_job(job_id))

        return Response(resp)


class FacilityTasksViewSet(BaseViewSet):
    @property
    def queues(self):
        return [facility_queue]

    def default_permission_classes(self):
        if self.action in ["list", "retrieve"]:
            return [FacilitySyncPermissions]

    @decorators.action(
        methods=["post"], detail=False, permission_classes=[FacilitySyncPermissions]
    )
    def startdataportalsync(self, request):
        """
        Initiate a PUSH sync with Kolibri Data Portal.
        """
        facility_id = validate_facility(request)
        sync_args = validate_sync_task(request)
        job_data = prepare_sync_job(
            facility_id,
            extra_metadata=prepare_sync_task(*sync_args, type="SYNCDATAPORTAL"),
        )
        job_id = facility_queue.enqueue(call_command, "sync", **job_data)

        resp = _job_to_response(facility_queue.fetch_job(job_id))
        return Response(resp)

    @decorators.action(methods=["post"], detail=False, permission_classes=[IsSuperuser])
    def startdataportalbulksync(self, request):
        """
        Initiate a PUSH sync with Kolibri Data Portal for ALL registered facilities.
        """
        responses = []
        facilities = Facility.objects.filter(dataset__registered=True).values_list(
            "id", "name"
        )

        for id, name in facilities:
            request.data.update(facility=id, facility_name=name)
            responses.append(self.startdataportalsync(request).data)

        return Response(responses)

    # Method needs to be available in Setup Wizard as well
    @decorators.action(
        methods=["post"],
        detail=False,
        permission_classes=[IsSuperuser | NotProvisionedCanPost],
    )
    def startpeerfacilityimport(self, request):
        """
        Initiate a PULL of a specific facility from another device.
        """

        baseurl, facility_id, username, password = validate_peer_sync_job(request)
        sync_args = validate_sync_task(request)
        job_data = prepare_peer_sync_job(
            baseurl,
            facility_id,
            username,
            password,
            no_push=True,
            no_provision=True,
            extra_metadata=prepare_sync_task(*sync_args, type="SYNCPEER/PULL"),
        )

        job_id = facility_queue.enqueue(call_command, "sync", **job_data)

        resp = _job_to_response(facility_queue.fetch_job(job_id))
        return Response(resp)

    @decorators.action(
        methods=["post"], detail=False, permission_classes=[FacilitySyncPermissions]
    )
    def startpeerfacilitysync(self, request):
        """
        Initiate a SYNC (PULL + PUSH) of a specific facility from another device.
        """
        baseurl, facility_id, username, password = validate_peer_sync_job(request)
        sync_args = validate_sync_task(request)
        job_data = prepare_peer_sync_job(
            baseurl,
            facility_id,
            username,
            password,
            extra_metadata=prepare_sync_task(*sync_args, type="SYNCPEER/FULL"),
        )
        job_id = facility_queue.enqueue(call_command, "sync", **job_data)

        resp = _job_to_response(facility_queue.fetch_job(job_id))
        return Response(resp)

    @decorators.action(methods=["post"], detail=False, permission_classes=[IsSuperuser])
    def startdeletefacility(self, request):
        """
        Initiate a task to delete a facility
        """
        try:
            facility_id = request.data.get("facility")
            if not facility_id:
                raise KeyError()
        except KeyError:
            raise ParseError(
                dict(code="INVALID_FACILITY", message="Missing `facility` parameter")
            )

        if not Facility.objects.filter(id=facility_id).exists():
            raise ValidationError(
                dict(code="INVALID_FACILITY", message="Facility doesn't exist")
            )

        if not Facility.objects.exclude(id=facility_id).exists():
            raise ValidationError(
                dict(
                    code="SOLE_FACILITY",
                    message="Cannot delete the sole facility on the device",
                )
            )

        if request.user.is_facility_user and request.user.facility_id == facility_id:
            raise ValidationError(
                dict(code="FACILITY_MEMBER", message="User is member of facility")
            )

        facility_name = Facility.objects.get(id=facility_id).name
        job_id = facility_queue.enqueue(
            call_command,
            "deletefacility",
            facility=facility_id,
            track_progress=True,
            noninteractive=True,
            cancellable=False,
            extra_metadata=dict(
                facility=facility_id,
                facility_name=facility_name,
                started_by=request.user.pk,
                started_by_username=request.user.username,
                type="DELETEFACILITY",
            ),
        )

        resp = _job_to_response(facility_queue.fetch_job(job_id))
        return Response(resp)


class ResourceGoneError(APIException):
    """
    API error for when a peer no longer is online
    """

    status_code = status.HTTP_410_GONE
    default_detail = "Unable to connect"


def prepare_sync_task(
    facility_id,
    user_id,
    username,
    facility_name,
    device_name,
    device_id,
    baseurl,
    **kwargs
):
    task_data = dict(
        facility=facility_id,
        started_by=user_id,
        started_by_username=username,
        sync_state=FacilitySyncState.PENDING,
        bytes_sent=0,
        bytes_received=0,
    )

    task_type = kwargs.get("type")
    if task_type in ["SYNCPEER/PULL", "SYNCPEER/FULL"]:
        # Extra metadata that can be passed from the client
        extra_task_data = dict(
            facility_name=facility_name,
            device_name=device_name,
            device_id=device_id,
            baseurl=baseurl,
        )
        task_data.update(extra_task_data)
    elif task_type == "SYNCDATAPORTAL":
        # Extra metadata that can be passed from the client
        extra_task_data = dict(facility_name=facility_name)
        task_data.update(extra_task_data)

    task_data.update(kwargs)
    return task_data


def validate_facility(request):
    # ensure we have the facility
    try:
        facility_id = request.data.get("facility")
        if not facility_id:
            raise KeyError()
    except KeyError:
        raise ParseError("Missing `facility` parameter")

    return facility_id


def validate_sync_task(request):
    facility_id = validate_facility(request)
    user_id = request.user.pk
    username = request.user.username
    facility_name = request.data.get("facility_name", "")
    device_name = request.data.get("device_name", "")
    device_id = request.data.get("device_id", "")
    baseurl = request.data.get("baseurl", "")
    return (
        facility_id,
        user_id,
        username,
        facility_name,
        device_name,
        device_id,
        baseurl,
    )


def prepare_sync_job(facility_id, **kwargs):

    job_data = dict(
        facility=facility_id,
        chunk_size=200,
        noninteractive=True,
        extra_metadata=dict(),
        track_progress=True,
        cancellable=False,
    )

    job_data.update(kwargs)
    return job_data


def validate_peer_sync_job(request):
    # validate the baseurl
    try:
        address = request.data.get("baseurl")
        if not address:
            raise KeyError()

        baseurl = NetworkClient(address=address).base_url
    except KeyError:
        raise ParseError("Missing `baseurl` parameter")
    except URLParseError:
        raise ParseError("Invalid URL")
    except NetworkLocationNotFound:
        raise ResourceGoneError()

    facility_id = validate_facility(request)

    username = request.data.get("username", None)
    password = request.data.get("password", None)

    return (baseurl, facility_id, username, password)


def prepare_peer_sync_job(baseurl, facility_id, username, password, **kwargs):
    """
    Initializes and validates connection to peer with username and password for the sync command. If
    already initialized, the username and password do not need to be supplied
    """
    # get the `user` for the sync command if present for provisioning
    user_id = kwargs.get("user", None)
    job_data = prepare_sync_job(facility_id, baseurl=baseurl, **kwargs)

    # call this in case user directly syncs without migrating database
    if not ScopeDefinition.objects.filter():
        call_command("loaddata", "scopedefinitions")

    controller = MorangoProfileController(PROFILE_FACILITY_DATA)
    network_connection = controller.create_network_connection(baseurl)

    # try to get the certificate, which will save it if successful
    try:
        # make sure we get the dataset ID
        dataset_id = get_dataset_id(
            baseurl, identifier=facility_id, noninteractive=True
        )

        # username and password are not required for this to succeed unless there is no cert
        get_client_and_server_certs(
            username,
            password,
            dataset_id,
            network_connection,
            user_id=user_id,
            facility_id=facility_id,
            noninteractive=True,
        )
    except (CommandError, HTTPError) as e:
        if not username and not password:
            raise PermissionDenied()
        else:
            raise AuthenticationFailed(e)

    return job_data


def prepare_soud_sync_job(baseurl, facility_id, user_id, **kwargs):
    """
    A SoUD sync requires that the device is already "registered" with the server, so there
    shouldn't be a need for username/password and the verification of those. This eliminates the
    validation to keep overhead low for automated single-user syncing. To initialize with a peer
    for a SoUD, use `prepare_peer_sync_job` with `user` keyword argument
    """
    kwargs.update(user=user_id)
    return prepare_sync_job(facility_id, baseurl=baseurl, **kwargs)


def _job_to_response(job):
    if not job:
        return {
            "type": None,
            "started_by": None,
            "status": State.SCHEDULED,
            "percentage": 0,
            "progress": [],
            "id": None,
            "cancellable": False,
            "clearable": False,
        }
    else:
        output = {
            "status": job.state,
            "exception": str(job.exception),
            "traceback": str(job.traceback),
            "percentage": job.percentage_progress,
            "id": job.job_id,
            "cancellable": job.cancellable,
            "clearable": job.state in [State.FAILED, State.CANCELED, State.COMPLETED],
        }
        output.update(job.extra_metadata)
        return output
