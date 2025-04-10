import asyncio
import dataclasses
import json
import logging
import traceback
from random import choice
from typing import AsyncIterator, Dict, List, Optional, Tuple

import aiohttp.web
from aiohttp.client import ClientResponse
from aiohttp.web import Request, Response, StreamResponse

import ray
from ray import NodeID
import ray.dashboard.consts as dashboard_consts
from ray.dashboard.consts import (
    GCS_RPC_TIMEOUT_SECONDS,
    DASHBOARD_AGENT_ADDR_NODE_ID_PREFIX,
    TRY_TO_GET_AGENT_INFO_INTERVAL_SECONDS,
    WAIT_AVAILABLE_AGENT_TIMEOUT,
)
from ray._common.utils import get_or_create_event_loop
from ray._private.ray_constants import env_bool, KV_NAMESPACE_DASHBOARD
from ray._private.runtime_env.packaging import (
    package_exists,
    pin_runtime_env_uri,
    upload_package_to_gcs,
)
from ray.dashboard.modules.job.common import (
    JobDeleteResponse,
    JobInfoStorageClient,
    JobLogsResponse,
    JobStopResponse,
    JobSubmitRequest,
    JobSubmitResponse,
    http_uri_components_to_uri,
)
from ray.dashboard.modules.job.pydantic_models import JobDetails, JobType
from ray.dashboard.modules.job.utils import (
    find_job_by_ids,
    get_driver_jobs,
    get_head_node_id,
    parse_and_validate_request,
)
from ray.dashboard.modules.version import CURRENT_VERSION, VersionResponse
from ray.dashboard.subprocesses.routes import SubprocessRouteTable as routes
from ray.dashboard.subprocesses.module import SubprocessModule
from ray.dashboard.subprocesses.utils import ResponseType
import time

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Feature flag controlling whether critical Ray Job control operations are performed
# exclusively by the Job Agent running on the Head node (or randomly sampled Worker one)
#
# NOTE: This flag serves as a temporary kill-switch and should be eventually cleaned up
RAY_JOB_AGENT_USE_HEAD_NODE_ONLY = env_bool("RAY_JOB_AGENT_USE_HEAD_NODE_ONLY", True)


class JobAgentSubmissionClient:
    """A local client for submitting and interacting with jobs on a specific node
    in the remote cluster.
    Submits requests over HTTP to the job agent on the specific node using the REST API.
    """

    def __init__(
        self,
        dashboard_agent_address: str,
    ):
        self._agent_address = dashboard_agent_address
        self._session = aiohttp.ClientSession()

    async def _raise_error(self, resp: ClientResponse):
        status = resp.status
        error_text = await resp.text()
        raise RuntimeError(f"Request failed with status code {status}: {error_text}.")

    async def submit_job_internal(self, req: JobSubmitRequest) -> JobSubmitResponse:
        logger.debug(f"Submitting job with submission_id={req.submission_id}.")

        async with self._session.post(
            f"{self._agent_address}/api/job_agent/jobs/", json=dataclasses.asdict(req)
        ) as resp:
            if resp.status == 200:
                result_json = await resp.json()
                return JobSubmitResponse(**result_json)
            else:
                await self._raise_error(resp)

    async def stop_job_internal(self, job_id: str) -> JobStopResponse:
        logger.debug(f"Stopping job with job_id={job_id}.")

        async with self._session.post(
            f"{self._agent_address}/api/job_agent/jobs/{job_id}/stop"
        ) as resp:
            if resp.status == 200:
                result_json = await resp.json()
                return JobStopResponse(**result_json)
            else:
                await self._raise_error(resp)

    async def delete_job_internal(self, job_id: str) -> JobDeleteResponse:
        logger.debug(f"Deleting job with job_id={job_id}.")

        async with self._session.delete(
            f"{self._agent_address}/api/job_agent/jobs/{job_id}"
        ) as resp:
            if resp.status == 200:
                result_json = await resp.json()
                return JobDeleteResponse(**result_json)
            else:
                await self._raise_error(resp)

    async def get_job_logs_internal(self, job_id: str) -> JobLogsResponse:
        async with self._session.get(
            f"{self._agent_address}/api/job_agent/jobs/{job_id}/logs"
        ) as resp:
            if resp.status == 200:
                result_json = await resp.json()
                return JobLogsResponse(**result_json)
            else:
                await self._raise_error(resp)

    async def tail_job_logs(self, job_id: str) -> AsyncIterator[str]:
        """Get an iterator that follows the logs of a job."""
        ws = await self._session.ws_connect(
            f"{self._agent_address}/api/job_agent/jobs/{job_id}/logs/tail"
        )

        while True:
            msg = await ws.receive()

            if msg.type == aiohttp.WSMsgType.TEXT:
                yield msg.data
            elif msg.type == aiohttp.WSMsgType.CLOSED:
                break
            elif msg.type == aiohttp.WSMsgType.ERROR:
                pass

    async def close(self, ignore_error=True):
        try:
            await self._session.close()
        except Exception:
            if not ignore_error:
                raise


class JobHead(SubprocessModule):
    """Runs on the head node of a Ray cluster and handles Ray Jobs APIs.

    NOTE(architkulkarni): Please keep this class in sync with the OpenAPI spec at
    `doc/source/cluster/running-applications/job-submission/openapi.yml`.
    We currently do not automatically check that the OpenAPI
    spec is in sync with the implementation. If any changes are made to the
    paths in the @route decorators or in the Responses returned by the
    methods (or any nested fields in the Responses), you will need to find the
    corresponding field of the OpenAPI yaml file and update it manually. Also,
    bump the version number in the yaml file and in this class's `get_version`.
    """

    # Time that we sleep while tailing logs while waiting for
    # the supervisor actor to start. We don't know which node
    # to read the logs from until then.
    WAIT_FOR_SUPERVISOR_ACTOR_INTERVAL_S = 1

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._job_info_client = None

        # To make sure that the internal KV is initialized by getting the lazy property
        assert self.gcs_client is not None
        assert ray.experimental.internal_kv._internal_kv_initialized()

        # It contains all `JobAgentSubmissionClient` that
        # `JobHead` has ever used, and will not be deleted
        # from it unless `JobAgentSubmissionClient` is no
        # longer available (the corresponding agent process is dead)
        # {node_id: JobAgentSubmissionClient}
        self._agents: Dict[NodeID, JobAgentSubmissionClient] = dict()

    async def get_target_agent(
        self, timeout_s: float = WAIT_AVAILABLE_AGENT_TIMEOUT
    ) -> JobAgentSubmissionClient:
        """
        Get a `JobAgentSubmissionClient`, which is a client for interacting with jobs
        via an agent process.

        Args:
            timeout_s: The timeout for the operation.

        Returns:
            A `JobAgentSubmissionClient` for interacting with jobs via an agent process.

        Raises:
            TimeoutError: If the operation times out.
        """
        if RAY_JOB_AGENT_USE_HEAD_NODE_ONLY:
            return await self._get_head_node_agent(timeout_s)

        return await self._pick_random_agent(timeout_s)

    async def _pick_random_agent(
        self, timeout_s: float
    ) -> Optional[JobAgentSubmissionClient]:
        """
        Try to disperse as much as possible to select one of
        the `CANDIDATE_AGENT_NUMBER` agents to solve requests.
        the agents will not pop from `self._agents` unless
        it's dead. Saved in `self._agents` is the agent that was
        used before.
        Strategy:
            1. if the number of `self._agents` has reached
               `CANDIDATE_AGENT_NUMBER`, randomly select one agent from
               `self._agents`.
            2. if not, randomly select one agent from all available agents,
               it is possible that the selected one already exists in
               `self._agents`.

        If there's no agent available at all, or there's exception, it will retry every
        `TRY_TO_GET_AGENT_INFO_INTERVAL_SECONDS` seconds indefinitely.

        Args:
            timeout_s: The timeout for the operation.

        Returns:
            A `JobAgentSubmissionClient` for interacting with jobs via an agent process.

        Raises:
            TimeoutError: If the operation times out.
        """
        start_time_s = time.time()
        last_exception = None
        while time.time() < start_time_s + timeout_s:
            try:
                return await self._pick_random_agent_once()
            except Exception as e:
                last_exception = e
                logger.exception(
                    f"Failed to pick a random agent, retrying in {TRY_TO_GET_AGENT_INFO_INTERVAL_SECONDS} seconds..."
                )
                await asyncio.sleep(TRY_TO_GET_AGENT_INFO_INTERVAL_SECONDS)
        raise TimeoutError(
            f"Failed to pick a random agent within {timeout_s} seconds. The last exception is {last_exception}"
        )

    async def _pick_random_agent_once(self) -> JobAgentSubmissionClient:
        """
        Query the internal kv for all agent infos, and pick agents randomly. May raise
        exception if there's no agent available at all or there's network error.
        """
        # NOTE: Following call will block until there's at least 1 agent info
        #       being populated from GCS
        agent_node_ids = await self._fetch_all_agent_node_ids()

        # delete dead agents.
        for dead_node in set(self._agents) - set(agent_node_ids):
            client = self._agents.pop(dead_node)
            await client.close()

        if len(self._agents) >= dashboard_consts.CANDIDATE_AGENT_NUMBER:
            node_id = choice(list(self._agents))
            return self._agents[node_id]
        else:
            # Randomly select one from among all agents, it is possible that
            # the selected one already exists in `self._agents`
            node_id = choice(list(agent_node_ids))

            if node_id not in self._agents:
                # Fetch agent info from InternalKV, and create a new
                # JobAgentSubmissionClient. May raise if the node_id is removed in
                # InternalKV after the _fetch_all_agent_node_ids, though unlikely.
                ip, http_port, _ = await self._fetch_agent_info(node_id)
                agent_http_address = f"http://{ip}:{http_port}"
                self._agents[node_id] = JobAgentSubmissionClient(agent_http_address)

            return self._agents[node_id]

    async def _get_head_node_agent_once(self) -> JobAgentSubmissionClient:
        head_node_id_hex = await get_head_node_id(self.gcs_aio_client)

        if not head_node_id_hex:
            raise Exception("Head node id has not yet been persisted in GCS")

        head_node_id = NodeID.from_hex(head_node_id_hex)

        if head_node_id not in self._agents:
            ip, http_port, _ = await self._fetch_agent_info(head_node_id)
            agent_http_address = f"http://{ip}:{http_port}"
            self._agents[head_node_id] = JobAgentSubmissionClient(agent_http_address)

        return self._agents[head_node_id]

    async def _get_head_node_agent(self, timeout_s: float) -> JobAgentSubmissionClient:
        """Retrieves HTTP client for `JobAgent` running on the Head node. If the head
        node does not have an agent, it will retry every
        `TRY_TO_GET_AGENT_INFO_INTERVAL_SECONDS` seconds indefinitely.

        Args:
            timeout_s: The timeout for the operation.

        Returns:
            A `JobAgentSubmissionClient` for interacting with jobs via the head node's agent process.

        Raises:
            TimeoutError: If the operation times out.
        """
        timeout_point = time.time() + timeout_s
        exception = None
        while time.time() < timeout_point:
            try:
                return await self._get_head_node_agent_once()
            except Exception as e:
                exception = e
                logger.exception(
                    f"Failed to get head node agent, retrying in {TRY_TO_GET_AGENT_INFO_INTERVAL_SECONDS} seconds..."
                )
                await asyncio.sleep(TRY_TO_GET_AGENT_INFO_INTERVAL_SECONDS)
        raise TimeoutError(
            f"Failed to get head node agent within {timeout_s} seconds. The last exception is {exception}"
        )

    async def _fetch_all_agent_node_ids(self) -> List[NodeID]:
        """
        Fetches all NodeIDs with agent infos in the cluster.

        May raise exception if there's no agent available at all or there's network error.
        Returns: List[NodeID]
        """
        keys = await self.gcs_aio_client.internal_kv_keys(
            f"{DASHBOARD_AGENT_ADDR_NODE_ID_PREFIX}".encode(),
            namespace=KV_NAMESPACE_DASHBOARD,
            timeout=GCS_RPC_TIMEOUT_SECONDS,
        )
        if not keys:
            # No agent keys found, retry
            raise Exception("No agents found in InternalKV.")
        return [
            NodeID.from_hex(key[len(DASHBOARD_AGENT_ADDR_NODE_ID_PREFIX) :].decode())
            for key in keys
        ]

    async def _fetch_agent_info(self, target_node_id: NodeID) -> Tuple[str, int, int]:
        """
        Fetches agent info by the Node ID. May raise exception if there's network error or the
        agent info is not found.

        Returns: (ip, http_port, grpc_port)
        """
        key = f"{DASHBOARD_AGENT_ADDR_NODE_ID_PREFIX}{target_node_id.hex()}"
        value = await self.gcs_aio_client.internal_kv_get(
            key,
            namespace=KV_NAMESPACE_DASHBOARD,
            timeout=GCS_RPC_TIMEOUT_SECONDS,
        )
        if not value:
            raise KeyError(
                f"Agent info not found in internal KV for node {target_node_id}. "
                "It's possible that the agent didn't launch successfully due to "
                "port conflicts or other issues. Please check `dashboard_agent.log` "
                "for more details."
            )
        return json.loads(value.decode())

    @routes.get("/api/version")
    async def get_version(self, req: Request) -> Response:
        # NOTE(edoakes): CURRENT_VERSION should be bumped and checked on the
        # client when we have backwards-incompatible changes.
        resp = VersionResponse(
            version=CURRENT_VERSION,
            ray_version=ray.__version__,
            ray_commit=ray.__commit__,
            session_name=self.session_name,
        )
        return Response(
            text=json.dumps(dataclasses.asdict(resp)),
            content_type="application/json",
            status=aiohttp.web.HTTPOk.status_code,
        )

    @routes.get("/api/packages/{protocol}/{package_name}")
    async def get_package(self, req: Request) -> Response:
        package_uri = http_uri_components_to_uri(
            protocol=req.match_info["protocol"],
            package_name=req.match_info["package_name"],
        )

        logger.debug(f"Adding temporary reference to package {package_uri}.")
        try:
            pin_runtime_env_uri(package_uri)
        except Exception:
            return Response(
                text=traceback.format_exc(),
                status=aiohttp.web.HTTPInternalServerError.status_code,
            )

        if not package_exists(package_uri):
            return Response(
                text=f"Package {package_uri} does not exist",
                status=aiohttp.web.HTTPNotFound.status_code,
            )

        return Response()

    @routes.put("/api/packages/{protocol}/{package_name}")
    async def upload_package(self, req: Request):
        package_uri = http_uri_components_to_uri(
            protocol=req.match_info["protocol"],
            package_name=req.match_info["package_name"],
        )
        logger.info(f"Uploading package {package_uri} to the GCS.")
        try:
            data = await req.read()
            await get_or_create_event_loop().run_in_executor(
                None,
                upload_package_to_gcs,
                package_uri,
                data,
            )
        except Exception:
            return Response(
                text=traceback.format_exc(),
                status=aiohttp.web.HTTPInternalServerError.status_code,
            )

        return Response(status=aiohttp.web.HTTPOk.status_code)

    @routes.post("/api/jobs/")
    async def submit_job(self, req: Request) -> Response:
        result = await parse_and_validate_request(req, JobSubmitRequest)
        # Request parsing failed, returned with Response object.
        if isinstance(result, Response):
            return result
        else:
            submit_request: JobSubmitRequest = result

        try:
            job_agent_client = await self.get_target_agent()
            resp = await job_agent_client.submit_job_internal(submit_request)
        except asyncio.TimeoutError:
            return Response(
                text="No available agent to submit job, please try again later.",
                status=aiohttp.web.HTTPInternalServerError.status_code,
            )
        except (TypeError, ValueError):
            return Response(
                text=traceback.format_exc(),
                status=aiohttp.web.HTTPBadRequest.status_code,
            )
        except Exception:
            return Response(
                text=traceback.format_exc(),
                status=aiohttp.web.HTTPInternalServerError.status_code,
            )

        return Response(
            text=json.dumps(dataclasses.asdict(resp)),
            content_type="application/json",
            status=aiohttp.web.HTTPOk.status_code,
        )

    @routes.post("/api/jobs/{job_or_submission_id}/stop")
    async def stop_job(self, req: Request) -> Response:
        job_or_submission_id = req.match_info["job_or_submission_id"]
        job = await find_job_by_ids(
            self.gcs_aio_client,
            self._job_info_client,
            job_or_submission_id,
        )
        if not job:
            return Response(
                text=f"Job {job_or_submission_id} does not exist",
                status=aiohttp.web.HTTPNotFound.status_code,
            )
        if job.type is not JobType.SUBMISSION:
            return Response(
                text="Can only stop submission type jobs",
                status=aiohttp.web.HTTPBadRequest.status_code,
            )

        try:
            job_agent_client = await self.get_target_agent()
            resp = await job_agent_client.stop_job_internal(job.submission_id)
        except Exception:
            return Response(
                text=traceback.format_exc(),
                status=aiohttp.web.HTTPInternalServerError.status_code,
            )

        return Response(
            text=json.dumps(dataclasses.asdict(resp)), content_type="application/json"
        )

    @routes.delete("/api/jobs/{job_or_submission_id}")
    async def delete_job(self, req: Request) -> Response:
        job_or_submission_id = req.match_info["job_or_submission_id"]
        job = await find_job_by_ids(
            self.gcs_aio_client,
            self._job_info_client,
            job_or_submission_id,
        )
        if not job:
            return Response(
                text=f"Job {job_or_submission_id} does not exist",
                status=aiohttp.web.HTTPNotFound.status_code,
            )
        if job.type is not JobType.SUBMISSION:
            return Response(
                text="Can only delete submission type jobs",
                status=aiohttp.web.HTTPBadRequest.status_code,
            )

        try:
            job_agent_client = await self.get_target_agent()
            resp = await job_agent_client.delete_job_internal(job.submission_id)
        except Exception:
            return Response(
                text=traceback.format_exc(),
                status=aiohttp.web.HTTPInternalServerError.status_code,
            )

        return Response(
            text=json.dumps(dataclasses.asdict(resp)), content_type="application/json"
        )

    @routes.get("/api/jobs/{job_or_submission_id}")
    async def get_job_info(self, req: Request) -> Response:
        job_or_submission_id = req.match_info["job_or_submission_id"]
        job = await find_job_by_ids(
            self.gcs_aio_client,
            self._job_info_client,
            job_or_submission_id,
        )
        if not job:
            return Response(
                text=f"Job {job_or_submission_id} does not exist",
                status=aiohttp.web.HTTPNotFound.status_code,
            )

        return Response(
            text=json.dumps(job.dict()),
            content_type="application/json",
        )

    # TODO(rickyx): This endpoint's logic is also mirrored in state API's endpoint.
    # We should eventually unify the backend logic (and keep the logic in sync before
    # that).
    @routes.get("/api/jobs/")
    async def list_jobs(self, req: Request) -> Response:
        (driver_jobs, submission_job_drivers), submission_jobs = await asyncio.gather(
            get_driver_jobs(self.gcs_aio_client), self._job_info_client.get_all_jobs()
        )

        submission_jobs = [
            JobDetails(
                **dataclasses.asdict(job),
                submission_id=submission_id,
                job_id=submission_job_drivers.get(submission_id).id
                if submission_id in submission_job_drivers
                else None,
                driver_info=submission_job_drivers.get(submission_id),
                type=JobType.SUBMISSION,
            )
            for submission_id, job in submission_jobs.items()
        ]
        return Response(
            text=json.dumps(
                [
                    *[submission_job.dict() for submission_job in submission_jobs],
                    *[job_info.dict() for job_info in driver_jobs.values()],
                ]
            ),
            content_type="application/json",
        )

    @routes.get("/api/jobs/{job_or_submission_id}/logs")
    async def get_job_logs(self, req: Request) -> Response:
        job_or_submission_id = req.match_info["job_or_submission_id"]
        job = await find_job_by_ids(
            self.gcs_aio_client,
            self._job_info_client,
            job_or_submission_id,
        )
        if not job:
            return Response(
                text=f"Job {job_or_submission_id} does not exist",
                status=aiohttp.web.HTTPNotFound.status_code,
            )

        if job.type is not JobType.SUBMISSION:
            return Response(
                text="Can only get logs of submission type jobs",
                status=aiohttp.web.HTTPBadRequest.status_code,
            )

        try:
            job_agent_client = self.get_job_driver_agent_client(job)
            payload = (
                await job_agent_client.get_job_logs_internal(job.submission_id)
                if job_agent_client
                else JobLogsResponse("")
            )
            return Response(
                text=json.dumps(dataclasses.asdict(payload)),
                content_type="application/json",
            )
        except Exception:
            return Response(
                text=traceback.format_exc(),
                status=aiohttp.web.HTTPInternalServerError.status_code,
            )

    @routes.get(
        "/api/jobs/{job_or_submission_id}/logs/tail", resp_type=ResponseType.WEBSOCKET
    )
    async def tail_job_logs(self, req: Request) -> StreamResponse:
        job_or_submission_id = req.match_info["job_or_submission_id"]
        job = await find_job_by_ids(
            self.gcs_aio_client,
            self._job_info_client,
            job_or_submission_id,
        )
        if not job:
            return Response(
                text=f"Job {job_or_submission_id} does not exist",
                status=aiohttp.web.HTTPNotFound.status_code,
            )

        if job.type is not JobType.SUBMISSION:
            return Response(
                text="Can only get logs of submission type jobs",
                status=aiohttp.web.HTTPBadRequest.status_code,
            )

        ws = aiohttp.web.WebSocketResponse()
        await ws.prepare(req)

        driver_agent_http_address = None
        while driver_agent_http_address is None:
            job = await find_job_by_ids(
                self.gcs_aio_client,
                self._job_info_client,
                job_or_submission_id,
            )
            driver_agent_http_address = job.driver_agent_http_address
            status = job.status
            if status.is_terminal() and driver_agent_http_address is None:
                # Job exited before supervisor actor started.
                return ws

            await asyncio.sleep(self.WAIT_FOR_SUPERVISOR_ACTOR_INTERVAL_S)

        job_agent_client = self.get_job_driver_agent_client(job)

        async for lines in job_agent_client.tail_job_logs(job.submission_id):
            await ws.send_str(lines)

        return ws

    def get_job_driver_agent_client(
        self, job: JobDetails
    ) -> Optional[JobAgentSubmissionClient]:
        if job.driver_agent_http_address is None:
            return None

        driver_node_id = job.driver_node_id
        if driver_node_id not in self._agents:
            self._agents[driver_node_id] = JobAgentSubmissionClient(
                job.driver_agent_http_address
            )

        return self._agents[driver_node_id]

    async def run(self):
        await super().run()
        if not self._job_info_client:
            self._job_info_client = JobInfoStorageClient(self.gcs_aio_client)
