import hashlib
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import delete, func, insert, select
from sqlalchemy.exc import IntegrityError

from saorsa_db.models.deployment import Deployment, DeploymentEvent, DeploymentStatus
from saorsa_db.models.file_result import DeploymentFileResult, FileOpType
from saorsa_db.models.derived_metric import DeploymentDerivedMetric
from saorsa_db.models.gate_verdict import DeploymentGateVerdict
from saorsa_db.models.host_metric_bucket import (
    DISK_METRICS,
    METRIC_DISK_USED_BYTES,
    METRIC_RSS_BYTES,
    DeploymentHostMetricBucket,
)
from saorsa_db.models.host_result import DeploymentHostResult
from saorsa_db.models.op_record import (
    DeploymentDownloadRecord,
    DeploymentUploadRecord,
    OpRecordStatus,
)
from saorsa_db.models.migration import (
    Migration,
    MigrationStatus,
    MigrationVm,
    MigrationVmStatus,
)
from saorsa_db.models.prod_canary import (
    DeploymentProdCanaryChunk,
    DeploymentProdCanaryLoss,
    DeploymentProdCanaryProbe,
    DeploymentProdCanaryUnavailability,
    DeploymentProdCanaryUpload,
    ProdCanaryChunkRole,
    ProdCanaryInitialVerifyStatus,
    ProdCanaryProbeStatus,
)
from saorsa_db.models.prod_disk_survey import (
    ProdDiskSurvey,
    ProdDiskSurveyService,
)
from saorsa_db.models.prod_download_result import (
    DeploymentProdDownloadResult,
    ProdDownloadStatus,
)
from saorsa_db.models.prod_file_pool import (
    NON_BLOCKING_STATUSES,
    SELF_RESOLVING_SOURCES,
    ClaimSource,
    ClaimStatus,
    PoolFileKind,
    ProdFilePool,
    ProdFilePoolClaim,
    ProdFilePoolDigest,
    ProdFilePoolTag,
)
from saorsa_db.models.prod_service_audit import (
    ProdServiceAudit,
    ProdServiceAuditAction,
)
from saorsa_db.models.prod_upload_result import (
    DeploymentProdUploadResult,
    ProdUploadStatus,
)
from saorsa_db.models.release_candidate import (
    ReleaseCandidate,
    ReleaseCandidateCrate,
    ReleaseCandidatePR,
)
from saorsa_db.size_class import SizeClass, byte_range
from saorsa_db.size_class import ordered as size_class_ordered


class DeploymentRepository(ABC):
    @abstractmethod
    def create(
        self,
        name: str,
        deploy_args: dict,
        workflow_run_id: int | None = None,
    ) -> Deployment:
        ...

    @abstractmethod
    def get(self, deployment_id: int) -> Deployment | None:
        ...

    @abstractmethod
    def get_by_name(self, name: str) -> Deployment | None:
        """Get the most recent deployment with the given name."""
        ...

    @abstractmethod
    def get_active_by_name(self, name: str) -> Deployment | None:
        """Get the most recent non-terminal deployment with the given name."""
        ...

    @abstractmethod
    def list_all(self) -> list[Deployment]:
        ...

    @abstractmethod
    def list_active(self) -> list[Deployment]:
        ...

    @abstractmethod
    def update_status(
        self, deployment_id: int, status: DeploymentStatus
    ) -> Deployment:
        ...

    @abstractmethod
    def merge_deploy_args(
        self, deployment_id: int, updates: dict
    ) -> Deployment:
        """Shallow-merge ``updates`` into the deployment's ``deploy_args`` JSON."""
        ...

    @abstractmethod
    def set_slack_message(
        self, deployment_id: int, channel: str, message_ts: str
    ) -> Deployment:
        """Record the Slack (channel, message ts) of the deployment's
        ``slack-post`` so a later ``record-results`` can thread-reply to it."""
        ...

    @abstractmethod
    def add_event(
        self,
        deployment_id: int,
        event_type: str,
        details: dict | None = None,
    ) -> DeploymentEvent:
        ...

    @abstractmethod
    def delete(self, deployment_id: int) -> None:
        ...

    @abstractmethod
    def upsert_file_result(
        self,
        deployment_id: int,
        client_hostname: str,
        op_type: FileOpType,
        file_size_bytes: int,
        first_started_at: datetime,
        last_finished_at: datetime,
        success_count: int,
        failure_count: int,
        avg_duration_ms: int | None,
        min_duration_ms: int | None,
        max_duration_ms: int | None,
    ) -> DeploymentFileResult:
        ...

    @abstractmethod
    def list_file_results_for_deployment(
        self, deployment_id: int
    ) -> list[DeploymentFileResult]:
        ...

    @abstractmethod
    def upsert_upload_record(
        self,
        deployment_id: int,
        client_hostname: str,
        region: str | None,
        file_size_bytes: int,
        status: OpRecordStatus,
        start_ns: int,
        influx_time: datetime,
        started_at: datetime,
        finished_at: datetime | None,
        duration_ms: int | None,
        network_profile: str | None = None,
    ) -> DeploymentUploadRecord:
        ...

    @abstractmethod
    def upsert_download_record(
        self,
        deployment_id: int,
        client_hostname: str,
        region: str | None,
        file_size_bytes: int,
        status: OpRecordStatus,
        start_ns: int,
        influx_time: datetime,
        started_at: datetime,
        finished_at: datetime | None,
        duration_ms: int | None,
        network_profile: str | None = None,
    ) -> DeploymentDownloadRecord:
        ...

    @abstractmethod
    def list_upload_records_for_deployment(
        self, deployment_id: int
    ) -> list[DeploymentUploadRecord]:
        ...

    @abstractmethod
    def list_download_records_for_deployment(
        self, deployment_id: int
    ) -> list[DeploymentDownloadRecord]:
        ...

    @abstractmethod
    def upsert_host_result(
        self,
        deployment_id: int,
        host_metrics: list,
        metrics_started_at: datetime,
        metrics_ended_at: datetime,
        summary: str | None = None,
    ) -> DeploymentHostResult:
        ...

    @abstractmethod
    def get_host_result_for_deployment(
        self, deployment_id: int
    ) -> DeploymentHostResult | None:
        ...

    @abstractmethod
    def set_host_result_summary(
        self, deployment_id: int, summary: str
    ) -> DeploymentHostResult:
        ...

    @abstractmethod
    def replace_host_metric_buckets(
        self, deployment_id: int, buckets: list[dict]
    ) -> int:
        ...

    @abstractmethod
    def count_host_metric_buckets(self, deployment_id: int) -> int:
        ...

    @abstractmethod
    def get_host_metric_window(
        self,
        deployment_id: int,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[dict]:
        ...

    @abstractmethod
    def get_host_metric_bucket_span(
        self, deployment_id: int
    ) -> tuple[datetime, datetime] | None:
        ...

    @abstractmethod
    def delete_host_metric_buckets_before(
        self, cutoff: datetime, deployment_id: int | None = None
    ) -> int:
        ...

    @abstractmethod
    def summarize_host_metric_buckets(
        self, cutoff: datetime | None = None
    ) -> list[dict]:
        ...

    @abstractmethod
    def get_host_metric_buckets_in_window(
        self,
        deployment_id: int,
        start: datetime | None = None,
        end: datetime | None = None,
        metric: str | None = None,
    ) -> list[dict]:
        ...

    @abstractmethod
    def get_host_metric_endpoints(
        self,
        deployment_id: int,
        metric: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[dict]:
        ...

    @abstractmethod
    def upsert_derived_metric(
        self,
        deployment_id: int,
        metric: str,
        window_label: str,
        window_start: datetime,
        window_end: datetime,
        unit: str,
        source: str,
        value: float | None = None,
        numerator: float | None = None,
        denominator: float | None = None,
        details: dict | None = None,
    ) -> DeploymentDerivedMetric:
        ...

    @abstractmethod
    def list_derived_metrics_for_deployment(
        self, deployment_id: int, window_label: str | None = None
    ) -> list[DeploymentDerivedMetric]:
        ...

    @abstractmethod
    def delete_derived_metrics(
        self, deployment_id: int, window_label: str | None = None
    ) -> int:
        ...

    @abstractmethod
    def upsert_gate_verdict(
        self,
        deployment_id: int,
        baseline_deployment_id: int,
        window_label: str,
        overall: str,
        threshold_config_version: int,
        verdict: dict,
        report_markdown: str | None = None,
    ) -> DeploymentGateVerdict:
        ...

    @abstractmethod
    def list_gate_verdicts_for_deployment(
        self, deployment_id: int
    ) -> list[DeploymentGateVerdict]:
        ...

    @abstractmethod
    def get_gate_verdict(
        self, deployment_id: int, baseline_deployment_id: int, window_label: str
    ) -> DeploymentGateVerdict | None:
        ...

    @abstractmethod
    def upsert_prod_upload_result(
        self,
        deployment_id: int,
        client_hostname: str,
        region: str,
        source_url: str,
        file_name: str,
        file_size_bytes: int,
        status: ProdUploadStatus,
        address: str | None,
        chunks_stored: int,
        chunks_failed: int,
        total_chunks: int,
        duration_ms: int | None,
        upload_secs: float | None,
        started_at: datetime,
        finished_at: datetime,
        reason: str | None = None,
        storage_cost_atto: Optional[Decimal] = None,
        gas_cost_wei: Optional[Decimal] = None,
    ) -> DeploymentProdUploadResult:
        ...

    @abstractmethod
    def list_prod_upload_results_for_deployment(
        self, deployment_id: int
    ) -> list[DeploymentProdUploadResult]:
        ...

    @abstractmethod
    def list_all_prod_upload_results(
        self, failed_only: bool = False
    ) -> list[DeploymentProdUploadResult]:
        ...

    @abstractmethod
    def upsert_prod_download_result(
        self,
        deployment_id: int,
        client_hostname: str,
        region: str,
        address: str,
        status: ProdDownloadStatus,
        output_path: str | None,
        file_size_bytes: int | None,
        duration_ms: int | None,
        download_secs: float | None,
        started_at: datetime,
        finished_at: datetime,
        reason: str | None = None,
    ) -> DeploymentProdDownloadResult:
        ...

    @abstractmethod
    def list_prod_download_results_for_deployment(
        self, deployment_id: int
    ) -> list[DeploymentProdDownloadResult]:
        ...

    @abstractmethod
    def upsert_prod_canary_upload(
        self,
        canary_id,
        deployment_id: int,
        source_url: str,
        file_name: str,
        file_size_bytes: int,
        top_address: str,
        num_chunks: int,
        uploaded_at: datetime,
        initial_verify_status: ProdCanaryInitialVerifyStatus,
        storage_cost_atto: Optional[Decimal] = None,
        gas_cost_wei: Optional[Decimal] = None,
        chunk_attempts_total: Optional[int] = None,
    ) -> DeploymentProdCanaryUpload:
        ...

    @abstractmethod
    def upsert_prod_canary_chunk(
        self,
        canary_id,
        chunk_address: str,
        chunk_role: ProdCanaryChunkRole,
    ) -> DeploymentProdCanaryChunk:
        ...

    @abstractmethod
    def upsert_prod_canary_probe(
        self,
        canary_id,
        probed_at: datetime,
        probed_by_host: str,
        status: ProdCanaryProbeStatus,
        missing_chunk_address: str | None,
        terminal_peer_outcomes: dict | None,
        duration_ms: int | None,
    ) -> DeploymentProdCanaryProbe:
        ...

    @abstractmethod
    def list_prod_canary_uploads_for_deployment(
        self, deployment_id: int
    ) -> list[DeploymentProdCanaryUpload]:
        ...

    @abstractmethod
    def list_all_prod_canary_uploads(self) -> list[DeploymentProdCanaryUpload]:
        """Every canary upload across all deployments, oldest first.

        Canary uploads live in their own table and are NOT in
        ``deployment_prod_upload_results`` — so anything asking "what is
        already on the production network?" that reads only the latter misses
        the larger share of it. Measured on 2026-07-30: 1006 canary files
        against 538 from prod-uploaders runs.
        """
        ...

    @abstractmethod
    def list_prod_canary_probes_for_canary(
        self, canary_id
    ) -> list[DeploymentProdCanaryProbe]:
        ...

    @abstractmethod
    def list_prod_canary_probes_for_deployment(
        self, deployment_id: int
    ) -> list[DeploymentProdCanaryProbe]:
        ...

    @abstractmethod
    def upsert_prod_canary_loss(
        self,
        canary_id,
        first_failed_probe_at: datetime,
        confirmed_lost_at: datetime,
        missing_chunk_address: str,
        hours_to_first_failure: float,
        hours_to_confirmed_loss: float,
    ) -> DeploymentProdCanaryLoss:
        ...

    @abstractmethod
    def delete_prod_canary_loss(self, canary_id) -> None:
        ...

    @abstractmethod
    def list_prod_canary_loss_for_deployment(
        self, deployment_id: int
    ) -> list[DeploymentProdCanaryLoss]:
        ...

    @abstractmethod
    def replace_prod_canary_unavailability(
        self, canary_id, episodes: list[dict]
    ) -> None:
        ...

    @abstractmethod
    def list_prod_canary_unavailability_for_deployment(
        self, deployment_id: int
    ) -> list[DeploymentProdCanaryUnavailability]:
        ...

    @abstractmethod
    def deployment_ids_with_results(self) -> set[int]:
        """Return the set of deployment ids that have any persisted result.

        A deployment counts as having results if it has at least one row in
        any of: ``deployment_file_results``, ``deployment_prod_upload_results``,
        or ``deployment_host_results``.
        """
        ...


class SqlAlchemyDeploymentRepository(DeploymentRepository):
    def __init__(self, session):
        self.session = session

    def create(
        self,
        name: str,
        deploy_args: dict,
        workflow_run_id: int | None = None,
    ) -> Deployment:
        deployment = Deployment(
            name=name,
            deploy_args=deploy_args,
            workflow_run_id=workflow_run_id,
            status=DeploymentStatus.PENDING,
        )
        self.session.add(deployment)
        self.session.flush()

        event = DeploymentEvent(
            deployment_id=deployment.id,
            event_type="created",
            details={"deploy_args": deploy_args},
        )
        self.session.add(event)
        self.session.commit()
        return deployment

    def get(self, deployment_id: int) -> Deployment | None:
        return self.session.get(Deployment, deployment_id)

    def get_by_name(self, name: str) -> Deployment | None:
        return (
            self.session.query(Deployment)
            .filter(Deployment.name == name)
            .order_by(Deployment.created_at.desc())
            .first()
        )

    def get_active_by_name(self, name: str) -> Deployment | None:
        terminal_statuses = [DeploymentStatus.DESTROYED, DeploymentStatus.FAILED]
        return (
            self.session.query(Deployment)
            .filter(
                Deployment.name == name,
                Deployment.status.notin_(terminal_statuses),
            )
            .order_by(Deployment.created_at.desc())
            .first()
        )

    def list_all(self) -> list[Deployment]:
        return (
            self.session.query(Deployment)
            .order_by(Deployment.created_at.desc())
            .all()
        )

    def list_active(self) -> list[Deployment]:
        active_statuses = [
            DeploymentStatus.PENDING,
            DeploymentStatus.RUNNING,
            DeploymentStatus.DESTROYING,
        ]
        return (
            self.session.query(Deployment)
            .filter(Deployment.status.in_(active_statuses))
            .order_by(Deployment.created_at.desc())
            .all()
        )

    def update_status(
        self, deployment_id: int, status: DeploymentStatus
    ) -> Deployment:
        deployment = self.session.get(Deployment, deployment_id)
        if deployment is None:
            raise ValueError(f"Deployment {deployment_id} not found")
        deployment.status = status
        deployment.updated_at = datetime.now(timezone.utc)
        self.session.commit()
        return deployment

    def merge_deploy_args(
        self, deployment_id: int, updates: dict
    ) -> Deployment:
        deployment = self.session.get(Deployment, deployment_id)
        if deployment is None:
            raise ValueError(f"Deployment {deployment_id} not found")
        # Reassign rather than mutate in place — SQLAlchemy's JSON column
        # does not track mutations on the underlying dict by default.
        deployment.deploy_args = {**(deployment.deploy_args or {}), **updates}
        deployment.updated_at = datetime.now(timezone.utc)
        self.session.commit()
        return deployment

    def set_slack_message(
        self, deployment_id: int, channel: str, message_ts: str
    ) -> Deployment:
        deployment = self.session.get(Deployment, deployment_id)
        if deployment is None:
            raise ValueError(f"Deployment {deployment_id} not found")
        deployment.slack_channel = channel
        deployment.slack_message_ts = message_ts
        deployment.updated_at = datetime.now(timezone.utc)
        self.session.commit()
        return deployment

    def add_event(
        self,
        deployment_id: int,
        event_type: str,
        details: dict | None = None,
    ) -> DeploymentEvent:
        deployment = self.session.get(Deployment, deployment_id)
        if deployment is None:
            raise ValueError(f"Deployment {deployment_id} not found")
        event = DeploymentEvent(
            deployment_id=deployment_id,
            event_type=event_type,
            details=details,
        )
        self.session.add(event)
        self.session.commit()
        return event

    def delete(self, deployment_id: int) -> None:
        deployment = self.session.get(Deployment, deployment_id)
        if deployment is None:
            raise ValueError(f"Deployment {deployment_id} not found")
        self.session.delete(deployment)
        self.session.commit()

    def upsert_file_result(
        self,
        deployment_id: int,
        client_hostname: str,
        op_type: FileOpType,
        file_size_bytes: int,
        first_started_at: datetime,
        last_finished_at: datetime,
        success_count: int,
        failure_count: int,
        avg_duration_ms: int | None,
        min_duration_ms: int | None,
        max_duration_ms: int | None,
    ) -> DeploymentFileResult:
        existing = (
            self.session.query(DeploymentFileResult)
            .filter(
                DeploymentFileResult.deployment_id == deployment_id,
                DeploymentFileResult.client_hostname == client_hostname,
                DeploymentFileResult.op_type == op_type,
                DeploymentFileResult.file_size_bytes == file_size_bytes,
            )
            .one_or_none()
        )
        if existing is None:
            existing = DeploymentFileResult(
                deployment_id=deployment_id,
                client_hostname=client_hostname,
                op_type=op_type,
                file_size_bytes=file_size_bytes,
                first_started_at=first_started_at,
                last_finished_at=last_finished_at,
                success_count=success_count,
                failure_count=failure_count,
                avg_duration_ms=avg_duration_ms,
                min_duration_ms=min_duration_ms,
                max_duration_ms=max_duration_ms,
            )
            self.session.add(existing)
        else:
            existing.first_started_at = first_started_at
            existing.last_finished_at = last_finished_at
            existing.success_count = success_count
            existing.failure_count = failure_count
            existing.avg_duration_ms = avg_duration_ms
            existing.min_duration_ms = min_duration_ms
            existing.max_duration_ms = max_duration_ms
            existing.recorded_at = datetime.now(timezone.utc)
        self.session.commit()
        return existing

    def list_file_results_for_deployment(
        self, deployment_id: int
    ) -> list[DeploymentFileResult]:
        return (
            self.session.query(DeploymentFileResult)
            .filter(DeploymentFileResult.deployment_id == deployment_id)
            .order_by(
                DeploymentFileResult.client_hostname,
                DeploymentFileResult.op_type,
                DeploymentFileResult.file_size_bytes,
            )
            .all()
        )

    def _upsert_op_record(
        self,
        model_cls,
        deployment_id: int,
        client_hostname: str,
        region: str | None,
        file_size_bytes: int,
        status: OpRecordStatus,
        start_ns: int,
        influx_time: datetime,
        started_at: datetime,
        finished_at: datetime | None,
        duration_ms: int | None,
        network_profile: str | None = None,
    ):
        existing = (
            self.session.query(model_cls)
            .filter(
                model_cls.deployment_id == deployment_id,
                model_cls.client_hostname == client_hostname,
                model_cls.start_ns == start_ns,
            )
            .one_or_none()
        )
        if existing is None:
            existing = model_cls(
                deployment_id=deployment_id,
                client_hostname=client_hostname,
                region=region,
                file_size_bytes=file_size_bytes,
                status=status,
                start_ns=start_ns,
                influx_time=influx_time,
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=duration_ms,
                network_profile=network_profile,
            )
            self.session.add(existing)
        else:
            existing.region = region
            existing.file_size_bytes = file_size_bytes
            existing.status = status
            existing.influx_time = influx_time
            existing.started_at = started_at
            existing.finished_at = finished_at
            existing.duration_ms = duration_ms
            existing.network_profile = network_profile
            existing.recorded_at = datetime.now(timezone.utc)
        self.session.commit()
        return existing

    def upsert_upload_record(
        self,
        deployment_id: int,
        client_hostname: str,
        region: str | None,
        file_size_bytes: int,
        status: OpRecordStatus,
        start_ns: int,
        influx_time: datetime,
        started_at: datetime,
        finished_at: datetime | None,
        duration_ms: int | None,
        network_profile: str | None = None,
    ) -> DeploymentUploadRecord:
        return self._upsert_op_record(
            DeploymentUploadRecord,
            deployment_id=deployment_id,
            client_hostname=client_hostname,
            region=region,
            file_size_bytes=file_size_bytes,
            status=status,
            start_ns=start_ns,
            influx_time=influx_time,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
            network_profile=network_profile,
        )

    def upsert_download_record(
        self,
        deployment_id: int,
        client_hostname: str,
        region: str | None,
        file_size_bytes: int,
        status: OpRecordStatus,
        start_ns: int,
        influx_time: datetime,
        started_at: datetime,
        finished_at: datetime | None,
        duration_ms: int | None,
        network_profile: str | None = None,
    ) -> DeploymentDownloadRecord:
        return self._upsert_op_record(
            DeploymentDownloadRecord,
            deployment_id=deployment_id,
            client_hostname=client_hostname,
            region=region,
            file_size_bytes=file_size_bytes,
            status=status,
            start_ns=start_ns,
            influx_time=influx_time,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
            network_profile=network_profile,
        )

    def list_upload_records_for_deployment(
        self, deployment_id: int
    ) -> list[DeploymentUploadRecord]:
        return (
            self.session.query(DeploymentUploadRecord)
            .filter(DeploymentUploadRecord.deployment_id == deployment_id)
            .order_by(
                DeploymentUploadRecord.client_hostname,
                DeploymentUploadRecord.start_ns,
            )
            .all()
        )

    def list_download_records_for_deployment(
        self, deployment_id: int
    ) -> list[DeploymentDownloadRecord]:
        return (
            self.session.query(DeploymentDownloadRecord)
            .filter(DeploymentDownloadRecord.deployment_id == deployment_id)
            .order_by(
                DeploymentDownloadRecord.client_hostname,
                DeploymentDownloadRecord.start_ns,
            )
            .all()
        )

    def upsert_host_result(
        self,
        deployment_id: int,
        host_metrics: list,
        metrics_started_at: datetime,
        metrics_ended_at: datetime,
        summary: str | None = None,
    ) -> DeploymentHostResult:
        existing = (
            self.session.query(DeploymentHostResult)
            .filter(DeploymentHostResult.deployment_id == deployment_id)
            .one_or_none()
        )
        if existing is None:
            existing = DeploymentHostResult(
                deployment_id=deployment_id,
                host_metrics=host_metrics,
                metrics_started_at=metrics_started_at,
                metrics_ended_at=metrics_ended_at,
                summary=summary,
            )
            self.session.add(existing)
        else:
            existing.host_metrics = host_metrics
            existing.metrics_started_at = metrics_started_at
            existing.metrics_ended_at = metrics_ended_at
            if summary is not None:
                existing.summary = summary
            existing.recorded_at = datetime.now(timezone.utc)
        self.session.commit()
        return existing

    def get_host_result_for_deployment(
        self, deployment_id: int
    ) -> DeploymentHostResult | None:
        return (
            self.session.query(DeploymentHostResult)
            .filter(DeploymentHostResult.deployment_id == deployment_id)
            .one_or_none()
        )

    def set_host_result_summary(
        self, deployment_id: int, summary: str
    ) -> DeploymentHostResult:
        existing = (
            self.session.query(DeploymentHostResult)
            .filter(DeploymentHostResult.deployment_id == deployment_id)
            .one_or_none()
        )
        if existing is None:
            raise ValueError(
                f"No host result recorded for deployment {deployment_id}"
            )
        existing.summary = summary
        existing.recorded_at = datetime.now(timezone.utc)
        self.session.commit()
        return existing

    # -- bucketed host metrics ------------------------------------------------

    def replace_host_metric_buckets(
        self, deployment_id: int, buckets: list[dict]
    ) -> int:
        """Replace every bucket row for a deployment, returning the row count.

        Replace rather than upsert, mirroring how ``upsert_host_result``
        replaces ``host_metrics`` wholesale: re-running ``record-analysis`` for
        a deployment re-states its whole stored window. NB a re-run with a
        *narrower* ``--window`` therefore shrinks what is stored -- the same
        hazard the rollup row already has.

        Rows are inserted in one bulk statement, not row-at-a-time through the
        ORM: a single run produces on the order of 100k rows.
        """
        self.session.execute(
            delete(DeploymentHostMetricBucket).where(
                DeploymentHostMetricBucket.deployment_id == deployment_id
            )
        )
        if buckets:
            now = datetime.now(timezone.utc)
            rows = [
                {
                    "deployment_id": deployment_id,
                    "host": b["host"],
                    "bucket_start": b["bucket_start"],
                    "metric": b["metric"],
                    "device": b.get("device"),
                    "volume_role": b.get("volume_role"),
                    "service": b.get("service"),
                    "avg": b.get("avg"),
                    "min": b.get("min"),
                    "max": b.get("max"),
                    "p50": b.get("p50"),
                    "p95": b.get("p95"),
                    "samples": b.get("samples") or 0,
                    "recorded_at": now,
                }
                for b in buckets
            ]
            self.session.execute(insert(DeploymentHostMetricBucket), rows)
        self.session.commit()
        return len(buckets)

    def count_host_metric_buckets(self, deployment_id: int) -> int:
        return (
            self.session.query(func.count(DeploymentHostMetricBucket.id))
            .filter(DeploymentHostMetricBucket.deployment_id == deployment_id)
            .scalar()
        ) or 0

    def get_host_metric_bucket_span(
        self, deployment_id: int
    ) -> tuple[datetime, datetime] | None:
        """Earliest and latest ``bucket_start`` stored for a deployment."""
        row = self.session.execute(
            select(
                func.min(DeploymentHostMetricBucket.bucket_start),
                func.max(DeploymentHostMetricBucket.bucket_start),
            ).where(DeploymentHostMetricBucket.deployment_id == deployment_id)
        ).one_or_none()
        if row is None or row[0] is None or row[1] is None:
            return None
        return (row[0], row[1])

    def get_host_metric_window(
        self,
        deployment_id: int,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[dict]:
        """Re-derive rollup-shaped per-host stats for an arbitrary window.

        Returns the same shape ``deployment_host_results.host_metrics`` holds --
        one dict per host with ``cpu_percent`` / ``mem_percent`` /
        ``bytes_in_per_sec`` / ``bytes_out_per_sec`` / ``disk_io_by_device`` /
        ``rss_by_service`` -- so a caller can treat a windowed re-derivation and
        the stored whole-run rollup interchangeably.

        The window is **half-open on** ``bucket_start`` (``start <= b < end``),
        so its granularity is the bucket granularity: a bucket is included
        whole or not at all. Both bounds are optional; omitting them returns the
        whole run, which is the case that must match the rollup row.

        ``avg`` is **sample-weighted** -- ``sum(avg * samples) / sum(samples)``,
        computed server-side. A plain mean of the per-bucket averages would be
        wrong whenever buckets hold unequal sample counts, which they do at run
        boundaries and wherever a scrape was missed.

        Returns an empty list for deployments recorded before bucketing existed;
        callers must degrade to the rollup rather than assume buckets exist.
        """
        weighted_sum = func.sum(
            DeploymentHostMetricBucket.avg * DeploymentHostMetricBucket.samples
        )
        total_samples = func.sum(DeploymentHostMetricBucket.samples)
        query = select(
            DeploymentHostMetricBucket.host,
            DeploymentHostMetricBucket.metric,
            DeploymentHostMetricBucket.device,
            DeploymentHostMetricBucket.volume_role,
            DeploymentHostMetricBucket.service,
            (weighted_sum / func.nullif(total_samples, 0)).label("avg"),
            func.min(DeploymentHostMetricBucket.min).label("min"),
            func.max(DeploymentHostMetricBucket.max).label("max"),
            # An ESTIMATE, not the window's true percentile -- percentiles do
            # not compose across buckets the way avg/min/max do. The mean of the
            # per-bucket medians converges on the window median for a stationary
            # series and is bounded by the bucket width otherwise. Flagged as
            # approximate in the returned stats so a consumer cannot mistake it
            # for the exact whole-run figure the rollup carries.
            func.avg(DeploymentHostMetricBucket.p50).label("p50"),
            func.avg(DeploymentHostMetricBucket.p95).label("p95"),
            total_samples.label("samples"),
            func.min(DeploymentHostMetricBucket.bucket_start).label("first_bucket"),
            func.max(DeploymentHostMetricBucket.bucket_start).label("last_bucket"),
        ).where(DeploymentHostMetricBucket.deployment_id == deployment_id)
        if start is not None:
            query = query.where(DeploymentHostMetricBucket.bucket_start >= start)
        if end is not None:
            query = query.where(DeploymentHostMetricBucket.bucket_start < end)
        query = query.group_by(
            DeploymentHostMetricBucket.host,
            DeploymentHostMetricBucket.metric,
            DeploymentHostMetricBucket.device,
            DeploymentHostMetricBucket.volume_role,
            DeploymentHostMetricBucket.service,
        )

        hosts: dict[str, dict] = {}
        for row in self.session.execute(query):
            entry = hosts.setdefault(
                row.host,
                {
                    "host": row.host,
                    "disk_io_by_device": {},
                    "disk_usage_by_mount": {},
                    "rss_by_service": {},
                    "_first": None,
                    "_last": None,
                },
            )
            if entry["_first"] is None or row.first_bucket < entry["_first"]:
                entry["_first"] = row.first_bucket
            if entry["_last"] is None or row.last_bucket > entry["_last"]:
                entry["_last"] = row.last_bucket

            stats = {
                "avg": float(row.avg) if row.avg is not None else None,
                "min": float(row.min) if row.min is not None else None,
                "max": float(row.max) if row.max is not None else None,
                "p50": float(row.p50) if row.p50 is not None else None,
                "p95": float(row.p95) if row.p95 is not None else None,
                # Always true on this path: a window's percentile is estimated
                # from per-bucket percentiles. The rollup's percentiles, computed
                # from every raw sample, carry no such flag.
                "percentile_approx": True,
                "samples": int(row.samples or 0),
            }
            if row.metric in DISK_METRICS:
                # Kept per-device, never pooled: a root disk and a node storage
                # volume are not interchangeable.
                device = entry["disk_io_by_device"].setdefault(
                    row.device or "", {"volume_role": row.volume_role}
                )
                if row.volume_role is not None:
                    device["volume_role"] = row.volume_role
                device[row.metric] = stats
            elif row.metric == METRIC_DISK_USED_BYTES:
                # Occupancy, keyed by mountpoint rather than device. Shaped like
                # the rollup's `disk_usage_by_mount` so a window and the rollup
                # stay interchangeable -- but WITHOUT `first`/`last`, which an
                # aggregate cannot recover. Growth over a window comes from
                # `get_host_metric_endpoints`, not from here.
                mount = entry["disk_usage_by_mount"].setdefault(
                    row.device or "", {"volume_role": row.volume_role}
                )
                if row.volume_role is not None:
                    mount["volume_role"] = row.volume_role
                mount["used_bytes"] = stats
            elif row.metric == METRIC_RSS_BYTES:
                # Keyed by service, never pooled into a host total: the signal
                # is one service leaking, which a 15-service mean would dilute.
                entry["rss_by_service"][row.service or ""] = stats
            else:
                entry[row.metric] = stats

        results = []
        for host in sorted(hosts):
            entry = hosts[host]
            first = entry.pop("_first")
            last = entry.pop("_last")
            entry["metrics_started_at"] = first.isoformat() if first else None
            entry["metrics_ended_at"] = last.isoformat() if last else None
            results.append(entry)
        return results

    def summarize_host_metric_buckets(
        self, cutoff: datetime | None = None
    ) -> list[dict]:
        """Per-deployment bucket counts and spans, for the prune command.

        With a ``cutoff``, only counts rows whose ``bucket_start`` is strictly
        older than it -- i.e. exactly what a prune at that cutoff would remove.
        """
        query = select(
            DeploymentHostMetricBucket.deployment_id,
            Deployment.name,
            func.count(DeploymentHostMetricBucket.id).label("rows"),
            func.min(DeploymentHostMetricBucket.bucket_start).label("first_bucket"),
            func.max(DeploymentHostMetricBucket.bucket_start).label("last_bucket"),
        ).join(
            Deployment, Deployment.id == DeploymentHostMetricBucket.deployment_id
        )
        if cutoff is not None:
            query = query.where(DeploymentHostMetricBucket.bucket_start < cutoff)
        query = query.group_by(
            DeploymentHostMetricBucket.deployment_id, Deployment.name
        ).order_by(DeploymentHostMetricBucket.deployment_id)
        return [
            {
                "deployment_id": row.deployment_id,
                "name": row.name,
                "rows": int(row.rows or 0),
                "first_bucket": row.first_bucket,
                "last_bucket": row.last_bucket,
            }
            for row in self.session.execute(query)
        ]

    def delete_host_metric_buckets_before(
        self, cutoff: datetime, deployment_id: int | None = None
    ) -> int:
        """Delete bucket rows older than ``cutoff``, returning the row count.

        Only touches ``deployment_host_metric_buckets``. The
        ``deployment_host_results`` rollup row is deliberately left alone, so a
        pruned deployment degrades to exactly the whole-run-only view it would
        have had before bucketing existed.
        """
        statement = delete(DeploymentHostMetricBucket).where(
            DeploymentHostMetricBucket.bucket_start < cutoff
        )
        if deployment_id is not None:
            statement = statement.where(
                DeploymentHostMetricBucket.deployment_id == deployment_id
            )
        result = self.session.execute(statement)
        self.session.commit()
        return result.rowcount or 0

    def get_host_metric_buckets_in_window(
        self,
        deployment_id: int,
        start: datetime | None = None,
        end: datetime | None = None,
        metric: str | None = None,
    ) -> list[dict]:
        """Raw bucket rows in a window, un-aggregated.

        ``get_host_metric_window`` collapses a window to avg/min/max, which is
        the right shape for reporting a rate but the wrong one for totalling
        bytes: a total is the sum of ``avg x bucket width`` over the individual
        buckets, and that sum is not recoverable from one pooled average once
        buckets with differing sample counts have been merged.

        Half-open on ``bucket_start`` (``start <= b < end``), matching
        ``get_host_metric_window`` so the two never disagree about which
        buckets a window contains.
        """
        query = select(
            DeploymentHostMetricBucket.host,
            DeploymentHostMetricBucket.metric,
            DeploymentHostMetricBucket.device,
            DeploymentHostMetricBucket.volume_role,
            DeploymentHostMetricBucket.service,
            DeploymentHostMetricBucket.bucket_start,
            DeploymentHostMetricBucket.avg,
            DeploymentHostMetricBucket.min,
            DeploymentHostMetricBucket.max,
            DeploymentHostMetricBucket.samples,
        ).where(DeploymentHostMetricBucket.deployment_id == deployment_id)
        if metric is not None:
            query = query.where(DeploymentHostMetricBucket.metric == metric)
        if start is not None:
            query = query.where(DeploymentHostMetricBucket.bucket_start >= start)
        if end is not None:
            query = query.where(DeploymentHostMetricBucket.bucket_start < end)
        query = query.order_by(DeploymentHostMetricBucket.bucket_start)
        return [
            {
                "host": row.host,
                "metric": row.metric,
                "device": row.device,
                "volume_role": row.volume_role,
                "service": row.service,
                "bucket_start": row.bucket_start,
                "avg": float(row.avg) if row.avg is not None else None,
                "min": float(row.min) if row.min is not None else None,
                "max": float(row.max) if row.max is not None else None,
                "samples": int(row.samples or 0),
            }
            for row in self.session.execute(query)
        ]

    def get_host_metric_endpoints(
        self,
        deployment_id: int,
        metric: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[dict]:
        """Per-host first and last bucket value of one metric in a window.

        ``get_host_metric_window`` aggregates a window down to avg/min/max,
        which is the right shape for a rate but destroys what an OCCUPANCY
        series is measured for: growth is ``last - first``, and neither endpoint
        survives an aggregate. This returns them.

        Each entry is ``{host, device, volume_role, service, first, last,
        first_bucket, last_bucket, buckets}``. ``first``/``last`` are the
        ``avg`` of the earliest and latest bucket in the window -- a bucket
        average rather than a raw reading, which for a slowly-growing series
        costs at most half a bucket width of resolution at each edge.

        Returns an empty list for deployments with no buckets; callers must
        degrade rather than assume.
        """
        query = (
            select(
                DeploymentHostMetricBucket.host,
                DeploymentHostMetricBucket.device,
                DeploymentHostMetricBucket.volume_role,
                DeploymentHostMetricBucket.service,
                DeploymentHostMetricBucket.bucket_start,
                DeploymentHostMetricBucket.avg,
            )
            .where(DeploymentHostMetricBucket.deployment_id == deployment_id)
            .where(DeploymentHostMetricBucket.metric == metric)
        )
        if start is not None:
            query = query.where(DeploymentHostMetricBucket.bucket_start >= start)
        if end is not None:
            query = query.where(DeploymentHostMetricBucket.bucket_start < end)
        query = query.order_by(DeploymentHostMetricBucket.bucket_start)

        # Grouped in Python rather than with a window function: the row count
        # here is one metric's buckets for one deployment, and DISTINCT ON is
        # Postgres-only while the tests (and any SQLite consumer) must work too.
        grouped: dict[tuple, dict] = {}
        for row in self.session.execute(query):
            key = (row.host, row.device, row.volume_role, row.service)
            entry = grouped.get(key)
            if entry is None:
                grouped[key] = {
                    "host": row.host,
                    "device": row.device,
                    "volume_role": row.volume_role,
                    "service": row.service,
                    "first": float(row.avg) if row.avg is not None else None,
                    "last": float(row.avg) if row.avg is not None else None,
                    "first_bucket": row.bucket_start,
                    "last_bucket": row.bucket_start,
                    "buckets": 1,
                }
                continue
            entry["buckets"] += 1
            if row.bucket_start < entry["first_bucket"]:
                entry["first_bucket"] = row.bucket_start
                entry["first"] = float(row.avg) if row.avg is not None else None
            if row.bucket_start > entry["last_bucket"]:
                entry["last_bucket"] = row.bucket_start
                entry["last"] = float(row.avg) if row.avg is not None else None
        return [grouped[key] for key in sorted(grouped, key=lambda k: tuple(
            "" if part is None else part for part in k
        ))]

    def upsert_derived_metric(
        self,
        deployment_id: int,
        metric: str,
        window_label: str,
        window_start: datetime,
        window_end: datetime,
        unit: str,
        source: str,
        value: float | None = None,
        numerator: float | None = None,
        denominator: float | None = None,
        details: dict | None = None,
    ) -> DeploymentDerivedMetric:
        """Insert or update the one row for this (deployment, metric, window).

        Recomputing must replace, never accumulate: a baseline comparison that
        silently picked one of several rival rows for the same window would be
        non-deterministic, which is exactly what the comparison-report command
        cannot tolerate.
        """
        existing = self.session.execute(
            select(DeploymentDerivedMetric)
            .where(DeploymentDerivedMetric.deployment_id == deployment_id)
            .where(DeploymentDerivedMetric.metric == metric)
            .where(DeploymentDerivedMetric.window_label == window_label)
        ).scalar_one_or_none()

        if existing is None:
            existing = DeploymentDerivedMetric(
                deployment_id=deployment_id,
                metric=metric,
                window_label=window_label,
            )
            self.session.add(existing)

        existing.window_start = window_start
        existing.window_end = window_end
        existing.unit = unit
        existing.source = source
        existing.value = value
        existing.numerator = numerator
        existing.denominator = denominator
        existing.details = details
        self.session.commit()
        self.session.refresh(existing)
        return existing

    def list_derived_metrics_for_deployment(
        self, deployment_id: int, window_label: str | None = None
    ) -> list[DeploymentDerivedMetric]:
        query = select(DeploymentDerivedMetric).where(
            DeploymentDerivedMetric.deployment_id == deployment_id
        )
        if window_label is not None:
            query = query.where(
                DeploymentDerivedMetric.window_label == window_label
            )
        query = query.order_by(
            DeploymentDerivedMetric.window_label,
            DeploymentDerivedMetric.metric,
        )
        return list(self.session.execute(query).scalars().all())

    def delete_derived_metrics(
        self, deployment_id: int, window_label: str | None = None
    ) -> int:
        statement = delete(DeploymentDerivedMetric).where(
            DeploymentDerivedMetric.deployment_id == deployment_id
        )
        if window_label is not None:
            statement = statement.where(
                DeploymentDerivedMetric.window_label == window_label
            )
        result = self.session.execute(statement)
        self.session.commit()
        return result.rowcount or 0

    def upsert_gate_verdict(
        self,
        deployment_id: int,
        baseline_deployment_id: int,
        window_label: str,
        overall: str,
        threshold_config_version: int,
        verdict: dict,
        report_markdown: str | None = None,
    ) -> DeploymentGateVerdict:
        """Insert or update the one verdict for this (run, baseline, window).

        Re-running a comparison replaces its previous answer rather than adding
        a second one. The comparison is a deterministic function of stored data
        and a threshold config, so two rows for the same key could only ever
        differ because the inputs changed -- and leaving both would make "the
        verdict for this run" ambiguous at exactly the moment it matters.
        """
        existing = self.session.execute(
            select(DeploymentGateVerdict)
            .where(DeploymentGateVerdict.deployment_id == deployment_id)
            .where(
                DeploymentGateVerdict.baseline_deployment_id
                == baseline_deployment_id
            )
            .where(DeploymentGateVerdict.window_label == window_label)
        ).scalar_one_or_none()

        if existing is None:
            existing = DeploymentGateVerdict(
                deployment_id=deployment_id,
                baseline_deployment_id=baseline_deployment_id,
                window_label=window_label,
            )
            self.session.add(existing)

        existing.overall = overall
        existing.threshold_config_version = threshold_config_version
        existing.verdict = verdict
        existing.report_markdown = report_markdown
        self.session.commit()
        self.session.refresh(existing)
        return existing

    def list_gate_verdicts_for_deployment(
        self, deployment_id: int
    ) -> list[DeploymentGateVerdict]:
        query = (
            select(DeploymentGateVerdict)
            .where(DeploymentGateVerdict.deployment_id == deployment_id)
            .order_by(
                DeploymentGateVerdict.window_label,
                DeploymentGateVerdict.baseline_deployment_id,
            )
        )
        return list(self.session.execute(query).scalars().all())

    def get_gate_verdict(
        self, deployment_id: int, baseline_deployment_id: int, window_label: str
    ) -> DeploymentGateVerdict | None:
        return self.session.execute(
            select(DeploymentGateVerdict)
            .where(DeploymentGateVerdict.deployment_id == deployment_id)
            .where(
                DeploymentGateVerdict.baseline_deployment_id
                == baseline_deployment_id
            )
            .where(DeploymentGateVerdict.window_label == window_label)
        ).scalar_one_or_none()

    def upsert_prod_upload_result(
        self,
        deployment_id: int,
        client_hostname: str,
        region: str,
        source_url: str,
        file_name: str,
        file_size_bytes: int,
        status: ProdUploadStatus,
        address: str | None,
        chunks_stored: int,
        chunks_failed: int,
        total_chunks: int,
        duration_ms: int | None,
        upload_secs: float | None,
        started_at: datetime,
        finished_at: datetime,
        reason: str | None = None,
        storage_cost_atto: Optional[Decimal] = None,
        gas_cost_wei: Optional[Decimal] = None,
    ) -> DeploymentProdUploadResult:
        existing = (
            self.session.query(DeploymentProdUploadResult)
            .filter(
                DeploymentProdUploadResult.deployment_id == deployment_id,
                DeploymentProdUploadResult.client_hostname == client_hostname,
                DeploymentProdUploadResult.source_url == source_url,
            )
            .one_or_none()
        )
        if existing is None:
            existing = DeploymentProdUploadResult(
                deployment_id=deployment_id,
                client_hostname=client_hostname,
                region=region,
                source_url=source_url,
                file_name=file_name,
                file_size_bytes=file_size_bytes,
                status=status,
                address=address,
                chunks_stored=chunks_stored,
                chunks_failed=chunks_failed,
                total_chunks=total_chunks,
                duration_ms=duration_ms,
                upload_secs=upload_secs,
                started_at=started_at,
                finished_at=finished_at,
                reason=reason,
                storage_cost_atto=storage_cost_atto,
                gas_cost_wei=gas_cost_wei,
            )
            self.session.add(existing)
        else:
            existing.region = region
            existing.file_name = file_name
            existing.file_size_bytes = file_size_bytes
            existing.status = status
            existing.address = address
            existing.chunks_stored = chunks_stored
            existing.chunks_failed = chunks_failed
            existing.total_chunks = total_chunks
            existing.duration_ms = duration_ms
            existing.upload_secs = upload_secs
            existing.started_at = started_at
            existing.finished_at = finished_at
            existing.reason = reason
            # Only overwrite cost when the caller actually has it — a re-record
            # against an older `ant` binary (which omits cost) shouldn't wipe a
            # value captured by an earlier run.
            if storage_cost_atto is not None:
                existing.storage_cost_atto = storage_cost_atto
            if gas_cost_wei is not None:
                existing.gas_cost_wei = gas_cost_wei
            existing.recorded_at = datetime.now(timezone.utc)
        self.session.commit()
        return existing

    def list_prod_upload_results_for_deployment(
        self, deployment_id: int
    ) -> list[DeploymentProdUploadResult]:
        return (
            self.session.query(DeploymentProdUploadResult)
            .filter(DeploymentProdUploadResult.deployment_id == deployment_id)
            .order_by(
                DeploymentProdUploadResult.client_hostname,
                DeploymentProdUploadResult.started_at,
            )
            .all()
        )

    def list_all_prod_upload_results(
        self, failed_only: bool = False
    ) -> list[DeploymentProdUploadResult]:
        query = self.session.query(DeploymentProdUploadResult)
        if failed_only:
            query = query.filter(
                DeploymentProdUploadResult.status != ProdUploadStatus.SUCCESS
            )
        else:
            query = query.filter(
                DeploymentProdUploadResult.status == ProdUploadStatus.SUCCESS
            )
        return query.order_by(
            DeploymentProdUploadResult.file_name,
            DeploymentProdUploadResult.started_at,
        ).all()

    def upsert_prod_download_result(
        self,
        deployment_id: int,
        client_hostname: str,
        region: str,
        address: str,
        status: ProdDownloadStatus,
        output_path: str | None,
        file_size_bytes: int | None,
        duration_ms: int | None,
        download_secs: float | None,
        started_at: datetime,
        finished_at: datetime,
        reason: str | None = None,
    ) -> DeploymentProdDownloadResult:
        existing = (
            self.session.query(DeploymentProdDownloadResult)
            .filter(
                DeploymentProdDownloadResult.deployment_id == deployment_id,
                DeploymentProdDownloadResult.client_hostname == client_hostname,
                DeploymentProdDownloadResult.address == address,
            )
            .one_or_none()
        )
        if existing is None:
            existing = DeploymentProdDownloadResult(
                deployment_id=deployment_id,
                client_hostname=client_hostname,
                region=region,
                address=address,
                status=status,
                output_path=output_path,
                file_size_bytes=file_size_bytes,
                duration_ms=duration_ms,
                download_secs=download_secs,
                started_at=started_at,
                finished_at=finished_at,
                reason=reason,
            )
            self.session.add(existing)
        else:
            existing.region = region
            existing.status = status
            existing.output_path = output_path
            existing.file_size_bytes = file_size_bytes
            existing.duration_ms = duration_ms
            existing.download_secs = download_secs
            existing.started_at = started_at
            existing.finished_at = finished_at
            existing.reason = reason
            existing.recorded_at = datetime.now(timezone.utc)
        self.session.commit()
        return existing

    def list_prod_download_results_for_deployment(
        self, deployment_id: int
    ) -> list[DeploymentProdDownloadResult]:
        return (
            self.session.query(DeploymentProdDownloadResult)
            .filter(DeploymentProdDownloadResult.deployment_id == deployment_id)
            .order_by(
                DeploymentProdDownloadResult.client_hostname,
                DeploymentProdDownloadResult.started_at,
            )
            .all()
        )

    def upsert_prod_canary_upload(
        self,
        canary_id,
        deployment_id: int,
        source_url: str,
        file_name: str,
        file_size_bytes: int,
        top_address: str,
        num_chunks: int,
        uploaded_at: datetime,
        initial_verify_status: ProdCanaryInitialVerifyStatus,
        storage_cost_atto: Optional[Decimal] = None,
        gas_cost_wei: Optional[Decimal] = None,
        chunk_attempts_total: Optional[int] = None,
    ) -> DeploymentProdCanaryUpload:
        existing = self.session.get(DeploymentProdCanaryUpload, canary_id)
        if existing is None:
            existing = DeploymentProdCanaryUpload(
                canary_id=canary_id,
                deployment_id=deployment_id,
                source_url=source_url,
                file_name=file_name,
                file_size_bytes=file_size_bytes,
                top_address=top_address,
                num_chunks=num_chunks,
                uploaded_at=uploaded_at,
                initial_verify_status=initial_verify_status,
                storage_cost_atto=storage_cost_atto,
                gas_cost_wei=gas_cost_wei,
                chunk_attempts_total=chunk_attempts_total,
            )
            self.session.add(existing)
        else:
            existing.deployment_id = deployment_id
            existing.source_url = source_url
            existing.file_name = file_name
            existing.file_size_bytes = file_size_bytes
            existing.top_address = top_address
            existing.num_chunks = num_chunks
            existing.uploaded_at = uploaded_at
            existing.initial_verify_status = initial_verify_status
            # Only overwrite cost fields if the caller supplied a value; this
            # preserves any historic data when re-running record-results on
            # rows that had cost added later.
            if storage_cost_atto is not None:
                existing.storage_cost_atto = storage_cost_atto
            if gas_cost_wei is not None:
                existing.gas_cost_wei = gas_cost_wei
            if chunk_attempts_total is not None:
                existing.chunk_attempts_total = chunk_attempts_total
            existing.recorded_at = datetime.now(timezone.utc)
        self.session.commit()
        return existing

    def upsert_prod_canary_chunk(
        self,
        canary_id,
        chunk_address: str,
        chunk_role: ProdCanaryChunkRole,
    ) -> DeploymentProdCanaryChunk:
        existing = self.session.get(
            DeploymentProdCanaryChunk, (canary_id, chunk_address)
        )
        if existing is None:
            existing = DeploymentProdCanaryChunk(
                canary_id=canary_id,
                chunk_address=chunk_address,
                chunk_role=chunk_role,
            )
            self.session.add(existing)
        else:
            existing.chunk_role = chunk_role
        self.session.commit()
        return existing

    def upsert_prod_canary_probe(
        self,
        canary_id,
        probed_at: datetime,
        probed_by_host: str,
        status: ProdCanaryProbeStatus,
        missing_chunk_address: str | None,
        terminal_peer_outcomes: dict | None,
        duration_ms: int | None,
    ) -> DeploymentProdCanaryProbe:
        existing = (
            self.session.query(DeploymentProdCanaryProbe)
            .filter(
                DeploymentProdCanaryProbe.canary_id == canary_id,
                DeploymentProdCanaryProbe.probed_at == probed_at,
                DeploymentProdCanaryProbe.probed_by_host == probed_by_host,
            )
            .one_or_none()
        )
        if existing is None:
            existing = DeploymentProdCanaryProbe(
                canary_id=canary_id,
                probed_at=probed_at,
                probed_by_host=probed_by_host,
                status=status,
                missing_chunk_address=missing_chunk_address,
                terminal_peer_outcomes=terminal_peer_outcomes,
                duration_ms=duration_ms,
            )
            self.session.add(existing)
        else:
            existing.status = status
            existing.missing_chunk_address = missing_chunk_address
            existing.terminal_peer_outcomes = terminal_peer_outcomes
            existing.duration_ms = duration_ms
            existing.recorded_at = datetime.now(timezone.utc)
        self.session.commit()
        return existing

    def list_prod_canary_uploads_for_deployment(
        self, deployment_id: int
    ) -> list[DeploymentProdCanaryUpload]:
        return (
            self.session.query(DeploymentProdCanaryUpload)
            .filter(DeploymentProdCanaryUpload.deployment_id == deployment_id)
            .order_by(DeploymentProdCanaryUpload.uploaded_at)
            .all()
        )

    def list_all_prod_canary_uploads(self) -> list[DeploymentProdCanaryUpload]:
        return (
            self.session.query(DeploymentProdCanaryUpload)
            .order_by(DeploymentProdCanaryUpload.uploaded_at)
            .all()
        )

    def list_prod_canary_probes_for_canary(
        self, canary_id
    ) -> list[DeploymentProdCanaryProbe]:
        return (
            self.session.query(DeploymentProdCanaryProbe)
            .filter(DeploymentProdCanaryProbe.canary_id == canary_id)
            .order_by(DeploymentProdCanaryProbe.probed_at)
            .all()
        )

    def list_prod_canary_probes_for_deployment(
        self, deployment_id: int
    ) -> list[DeploymentProdCanaryProbe]:
        return (
            self.session.query(DeploymentProdCanaryProbe)
            .join(
                DeploymentProdCanaryUpload,
                DeploymentProdCanaryUpload.canary_id
                == DeploymentProdCanaryProbe.canary_id,
            )
            .filter(DeploymentProdCanaryUpload.deployment_id == deployment_id)
            .order_by(
                DeploymentProdCanaryProbe.canary_id,
                DeploymentProdCanaryProbe.probed_at,
            )
            .all()
        )

    def upsert_prod_canary_loss(
        self,
        canary_id,
        first_failed_probe_at: datetime,
        confirmed_lost_at: datetime,
        missing_chunk_address: str,
        hours_to_first_failure: float,
        hours_to_confirmed_loss: float,
    ) -> DeploymentProdCanaryLoss:
        existing = self.session.get(DeploymentProdCanaryLoss, canary_id)
        if existing is None:
            existing = DeploymentProdCanaryLoss(
                canary_id=canary_id,
                first_failed_probe_at=first_failed_probe_at,
                confirmed_lost_at=confirmed_lost_at,
                missing_chunk_address=missing_chunk_address,
                hours_to_first_failure=hours_to_first_failure,
                hours_to_confirmed_loss=hours_to_confirmed_loss,
            )
            self.session.add(existing)
        else:
            existing.first_failed_probe_at = first_failed_probe_at
            existing.confirmed_lost_at = confirmed_lost_at
            existing.missing_chunk_address = missing_chunk_address
            existing.hours_to_first_failure = hours_to_first_failure
            existing.hours_to_confirmed_loss = hours_to_confirmed_loss
        self.session.commit()
        return existing

    def delete_prod_canary_loss(self, canary_id) -> None:
        existing = self.session.get(DeploymentProdCanaryLoss, canary_id)
        if existing is not None:
            self.session.delete(existing)
            self.session.commit()

    def list_prod_canary_loss_for_deployment(
        self, deployment_id: int
    ) -> list[DeploymentProdCanaryLoss]:
        return (
            self.session.query(DeploymentProdCanaryLoss)
            .join(
                DeploymentProdCanaryUpload,
                DeploymentProdCanaryUpload.canary_id
                == DeploymentProdCanaryLoss.canary_id,
            )
            .filter(DeploymentProdCanaryUpload.deployment_id == deployment_id)
            .order_by(DeploymentProdCanaryLoss.confirmed_lost_at)
            .all()
        )

    def replace_prod_canary_unavailability(
        self, canary_id, episodes: list[dict]
    ) -> None:
        """Replace all unavailability episodes for a canary with the given
        derived set. Idempotent: record-results re-derives the full episode
        list from the probe stream each run, so we delete-then-insert."""
        self.session.query(DeploymentProdCanaryUnavailability).filter(
            DeploymentProdCanaryUnavailability.canary_id == canary_id
        ).delete(synchronize_session=False)
        for ep in episodes:
            self.session.add(
                DeploymentProdCanaryUnavailability(
                    canary_id=canary_id,
                    episode_no=ep["episode_no"],
                    missing_chunk_address=ep["missing_chunk_address"],
                    first_failed_probe_at=ep["first_failed_probe_at"],
                    confirmed_unavailable_at=ep["confirmed_unavailable_at"],
                    recovered_at=ep.get("recovered_at"),
                    hours_to_confirmed=ep["hours_to_confirmed"],
                    unavailable_hours=ep.get("unavailable_hours"),
                    recovered=ep["recovered"],
                )
            )
        self.session.commit()

    def list_prod_canary_unavailability_for_deployment(
        self, deployment_id: int
    ) -> list[DeploymentProdCanaryUnavailability]:
        return (
            self.session.query(DeploymentProdCanaryUnavailability)
            .join(
                DeploymentProdCanaryUpload,
                DeploymentProdCanaryUpload.canary_id
                == DeploymentProdCanaryUnavailability.canary_id,
            )
            .filter(DeploymentProdCanaryUpload.deployment_id == deployment_id)
            .order_by(DeploymentProdCanaryUnavailability.confirmed_unavailable_at)
            .all()
        )

    def deployment_ids_with_results(self) -> set[int]:
        ids: set[int] = set()
        for model in (
            DeploymentFileResult,
            DeploymentProdUploadResult,
            DeploymentHostResult,
        ):
            ids.update(
                row[0]
                for row in self.session.query(model.deployment_id).distinct().all()
            )
        return ids


class MigrationRepository(ABC):
    @abstractmethod
    def create_migration(
        self,
        name: str,
        deployment_name: str,
        source_provider: str,
        source_region: str,
        target_provider: str,
        target_server_type: str,
        target_volume_size_gb: int,
        node_count_per_vm: int = 15,
    ) -> Migration:
        ...

    @abstractmethod
    def get_migration(self, migration_id: int) -> Migration | None:
        ...

    @abstractmethod
    def get_migration_by_name(self, name: str) -> Migration | None:
        ...

    @abstractmethod
    def list_migrations(self) -> list[Migration]:
        ...

    @abstractmethod
    def add_vms(
        self, migration_id: int, vm_records: list[dict]
    ) -> list[MigrationVm]:
        """Append MigrationVm rows to a migration. Each record dict supplies
        the source_* columns; target_* columns stay null until infra runs."""
        ...

    @abstractmethod
    def list_vms(
        self,
        migration_id: int,
        *,
        status: MigrationVmStatus | None = None,
    ) -> list[MigrationVm]:
        ...

    @abstractmethod
    def update_vm(self, vm_id: int, **fields) -> MigrationVm:
        """Update arbitrary columns on a MigrationVm. Status transitions
        should set the matching ``*_at`` timestamp via the caller."""
        ...

    @abstractmethod
    def update_migration_status(
        self, migration_id: int, status: MigrationStatus
    ) -> Migration:
        ...

    @abstractmethod
    def set_target_locations(
        self, migration_id: int, locations: dict[str, int]
    ) -> Migration:
        ...

    @abstractmethod
    def set_soak_completed(
        self, migration_id: int, completed_at: datetime
    ) -> Migration:
        ...

    @abstractmethod
    def delete_migration(self, migration_id: int) -> None:
        ...


class SqlAlchemyMigrationRepository(MigrationRepository):
    def __init__(self, session):
        self.session = session

    def create_migration(
        self,
        name: str,
        deployment_name: str,
        source_provider: str,
        source_region: str,
        target_provider: str,
        target_server_type: str,
        target_volume_size_gb: int,
        node_count_per_vm: int = 15,
    ) -> Migration:
        migration = Migration(
            name=name,
            deployment_name=deployment_name,
            source_provider=source_provider,
            source_region=source_region,
            target_provider=target_provider,
            target_server_type=target_server_type,
            target_volume_size_gb=target_volume_size_gb,
            node_count_per_vm=node_count_per_vm,
            status=MigrationStatus.PLANNING,
        )
        self.session.add(migration)
        self.session.commit()
        return migration

    def get_migration(self, migration_id: int) -> Migration | None:
        return self.session.get(Migration, migration_id)

    def get_migration_by_name(self, name: str) -> Migration | None:
        return (
            self.session.query(Migration)
            .filter(Migration.name == name)
            .one_or_none()
        )

    def list_migrations(self) -> list[Migration]:
        return (
            self.session.query(Migration)
            .order_by(Migration.created_at.desc())
            .all()
        )

    def add_vms(
        self, migration_id: int, vm_records: list[dict]
    ) -> list[MigrationVm]:
        migration = self.session.get(Migration, migration_id)
        if migration is None:
            raise ValueError(f"Migration {migration_id} not found")
        vms = []
        for record in vm_records:
            vm = MigrationVm(
                migration_id=migration_id,
                status=MigrationVmStatus.PLANNED,
                **record,
            )
            self.session.add(vm)
            vms.append(vm)
        self.session.commit()
        return vms

    def list_vms(
        self,
        migration_id: int,
        *,
        status: MigrationVmStatus | None = None,
    ) -> list[MigrationVm]:
        query = (
            self.session.query(MigrationVm)
            .filter(MigrationVm.migration_id == migration_id)
        )
        if status is not None:
            query = query.filter(MigrationVm.status == status)
        return query.order_by(MigrationVm.id).all()

    def update_vm(self, vm_id: int, **fields) -> MigrationVm:
        vm = self.session.get(MigrationVm, vm_id)
        if vm is None:
            raise ValueError(f"MigrationVm {vm_id} not found")
        for key, value in fields.items():
            if not hasattr(vm, key):
                raise ValueError(f"MigrationVm has no field {key!r}")
            setattr(vm, key, value)
        self.session.commit()
        return vm

    def update_migration_status(
        self, migration_id: int, status: MigrationStatus
    ) -> Migration:
        migration = self.session.get(Migration, migration_id)
        if migration is None:
            raise ValueError(f"Migration {migration_id} not found")
        migration.status = status
        migration.updated_at = datetime.now(timezone.utc)
        self.session.commit()
        return migration

    def set_target_locations(
        self, migration_id: int, locations: dict[str, int]
    ) -> Migration:
        migration = self.session.get(Migration, migration_id)
        if migration is None:
            raise ValueError(f"Migration {migration_id} not found")
        migration.target_locations = dict(locations)
        migration.updated_at = datetime.now(timezone.utc)
        self.session.commit()
        return migration

    def set_soak_completed(
        self, migration_id: int, completed_at: datetime
    ) -> Migration:
        migration = self.session.get(Migration, migration_id)
        if migration is None:
            raise ValueError(f"Migration {migration_id} not found")
        migration.soak_completed_at = completed_at
        migration.updated_at = datetime.now(timezone.utc)
        self.session.commit()
        return migration

    def delete_migration(self, migration_id: int) -> None:
        migration = self.session.get(Migration, migration_id)
        if migration is None:
            raise ValueError(f"Migration {migration_id} not found")
        self.session.delete(migration)
        self.session.commit()



class ReleaseCandidateRepository(ABC):
    @abstractmethod
    def create(
        self,
        *,
        binary_target: str,
        binary_version: str,
        rc_branch: str,
        notes: Optional[str] = None,
    ) -> ReleaseCandidate:
        ...

    @abstractmethod
    def add_crate(
        self,
        release_candidate_id: int,
        *,
        crate_name: str,
        version: str,
        repo_slug: str,
        tag: Optional[str] = None,
        tag_sha: Optional[str] = None,
        since_tag: Optional[str] = None,
        since_sha: Optional[str] = None,
        head_sha: Optional[str] = None,
    ) -> ReleaseCandidateCrate:
        ...

    @abstractmethod
    def add_prs(
        self, release_candidate_crate_id: int, prs: list[dict]
    ) -> list[ReleaseCandidatePR]:
        """Append PR rows under a crate. Each dict must include ``pr_number``
        (or ``number``); ``title``, ``author``, ``merged_at``, ``url`` are
        optional. Duplicate (crate_id, pr_number) pairs are skipped silently."""
        ...

    @abstractmethod
    def get(self, release_candidate_id: int) -> Optional[ReleaseCandidate]:
        ...

    @abstractmethod
    def get_by_binary_version(
        self, binary_target: str, binary_version: str
    ) -> Optional[ReleaseCandidate]:
        ...

    @abstractmethod
    def get_crate(
        self, release_candidate_id: int, crate_name: str
    ) -> Optional[ReleaseCandidateCrate]:
        ...

    @abstractmethod
    def list(
        self,
        *,
        binary_target: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[ReleaseCandidate]:
        ...

    @abstractmethod
    def delete(self, release_candidate_id: int) -> None:
        ...


class SqlAlchemyReleaseCandidateRepository(ReleaseCandidateRepository):
    def __init__(self, session):
        self.session = session

    def create(
        self,
        *,
        binary_target: str,
        binary_version: str,
        rc_branch: str,
        notes: Optional[str] = None,
    ) -> ReleaseCandidate:
        rc = ReleaseCandidate(
            binary_target=binary_target,
            binary_version=binary_version,
            rc_branch=rc_branch,
            notes=notes,
        )
        self.session.add(rc)
        self.session.commit()
        return rc

    def add_crate(
        self,
        release_candidate_id: int,
        *,
        crate_name: str,
        version: str,
        repo_slug: str,
        tag: Optional[str] = None,
        tag_sha: Optional[str] = None,
        since_tag: Optional[str] = None,
        since_sha: Optional[str] = None,
        head_sha: Optional[str] = None,
    ) -> ReleaseCandidateCrate:
        rc = self.session.get(ReleaseCandidate, release_candidate_id)
        if rc is None:
            raise ValueError(
                f"ReleaseCandidate {release_candidate_id} not found"
            )
        crate = ReleaseCandidateCrate(
            release_candidate_id=release_candidate_id,
            crate_name=crate_name,
            version=version,
            repo_slug=repo_slug,
            tag=tag,
            tag_sha=tag_sha,
            since_tag=since_tag,
            since_sha=since_sha,
            head_sha=head_sha,
        )
        self.session.add(crate)
        self.session.commit()
        return crate

    def add_prs(
        self, release_candidate_crate_id: int, prs: list[dict]
    ) -> list[ReleaseCandidatePR]:
        crate = self.session.get(
            ReleaseCandidateCrate, release_candidate_crate_id
        )
        if crate is None:
            raise ValueError(
                f"ReleaseCandidateCrate {release_candidate_crate_id} not found"
            )
        existing = {pr.pr_number for pr in crate.prs}
        created: list[ReleaseCandidatePR] = []
        for record in prs:
            number = record.get("pr_number") or record.get("number")
            if number is None:
                raise ValueError("PR record missing 'pr_number'")
            number = int(number)
            if number in existing:
                continue
            merged_at = record.get("merged_at")
            if isinstance(merged_at, str):
                merged_at = datetime.fromisoformat(merged_at.replace("Z", "+00:00"))
            row = ReleaseCandidatePR(
                release_candidate_crate_id=release_candidate_crate_id,
                pr_number=number,
                title=record.get("title"),
                author=record.get("author"),
                merged_at=merged_at,
                url=record.get("url"),
            )
            self.session.add(row)
            created.append(row)
            existing.add(number)
        self.session.commit()
        return created

    def get(self, release_candidate_id: int) -> Optional[ReleaseCandidate]:
        return self.session.get(ReleaseCandidate, release_candidate_id)

    def get_by_binary_version(
        self, binary_target: str, binary_version: str
    ) -> Optional[ReleaseCandidate]:
        return (
            self.session.query(ReleaseCandidate)
            .filter(
                ReleaseCandidate.binary_target == binary_target,
                ReleaseCandidate.binary_version == binary_version,
            )
            .one_or_none()
        )

    def get_crate(
        self, release_candidate_id: int, crate_name: str
    ) -> Optional[ReleaseCandidateCrate]:
        return (
            self.session.query(ReleaseCandidateCrate)
            .filter(
                ReleaseCandidateCrate.release_candidate_id
                == release_candidate_id,
                ReleaseCandidateCrate.crate_name == crate_name,
            )
            .one_or_none()
        )

    def list(
        self,
        *,
        binary_target: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[ReleaseCandidate]:
        query = self.session.query(ReleaseCandidate)
        if binary_target is not None:
            query = query.filter(ReleaseCandidate.binary_target == binary_target)
        query = query.order_by(ReleaseCandidate.recorded_at.desc())
        if limit is not None:
            query = query.limit(limit)
        return query.all()

    def delete(self, release_candidate_id: int) -> None:
        rc = self.session.get(ReleaseCandidate, release_candidate_id)
        if rc is None:
            raise ValueError(
                f"ReleaseCandidate {release_candidate_id} not found"
            )
        self.session.delete(rc)
        self.session.commit()


class ProdFleetRepository(ABC):
    """Standalone (no ``deployment_id``) records for production node fleets,
    keyed by the inventory identifier string (e.g. ``ant-prod-01``): disk-survey
    history and the service add/remove/reset audit trail."""

    @abstractmethod
    def create_disk_survey(
        self,
        inventory_name: str,
        surveyed_at: datetime,
        services: list[dict],
        host_disk: dict,
    ) -> ProdDiskSurvey:
        """Persist one survey: a header row plus one child row per service.

        ``services`` is a list of dicts with keys ``host``, ``provider``,
        ``service_name``, ``size_bytes``, ``active``, ``enabled``. ``host_disk``
        maps host name → ``{provider, total_bytes, used_bytes, free_bytes}``.
        Summary counts are derived here.
        """
        ...

    @abstractmethod
    def get_disk_survey(self, survey_id: int) -> ProdDiskSurvey | None:
        ...

    @abstractmethod
    def latest_disk_surveys(
        self, inventory_name: str, limit: int = 2
    ) -> list[ProdDiskSurvey]:
        """Most recent surveys for an inventory, newest first (for
        today-vs-yesterday diffs)."""
        ...

    @abstractmethod
    def record_service_audit(
        self,
        inventory_name: str,
        host: str,
        service_name: str,
        action: ProdServiceAuditAction,
        occurred_at: datetime,
        size_bytes: int | None = None,
        note: str | None = None,
    ) -> ProdServiceAudit:
        ...

    @abstractmethod
    def list_service_audit(
        self,
        inventory_name: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[ProdServiceAudit]:
        """Audit rows for an inventory in [since, until], oldest first."""
        ...


class SqlAlchemyProdFleetRepository(ProdFleetRepository):
    def __init__(self, session):
        self.session = session

    def create_disk_survey(
        self,
        inventory_name: str,
        surveyed_at: datetime,
        services: list[dict],
        host_disk: dict,
    ) -> ProdDiskSurvey:
        survey = ProdDiskSurvey(
            inventory_name=inventory_name,
            surveyed_at=surveyed_at,
            host_disk=host_disk or {},
            host_count=len(host_disk or {}),
            service_count=len(services),
            total_data_bytes=sum(int(s.get("size_bytes", 0)) for s in services),
        )
        for s in services:
            survey.services.append(
                ProdDiskSurveyService(
                    host=s["host"],
                    provider=s.get("provider"),
                    service_name=s["service_name"],
                    size_bytes=int(s.get("size_bytes", 0)),
                    active=bool(s.get("active", True)),
                    enabled=bool(s.get("enabled", True)),
                )
            )
        self.session.add(survey)
        self.session.commit()
        return survey

    def get_disk_survey(self, survey_id: int) -> ProdDiskSurvey | None:
        return self.session.get(ProdDiskSurvey, survey_id)

    def latest_disk_surveys(
        self, inventory_name: str, limit: int = 2
    ) -> list[ProdDiskSurvey]:
        return (
            self.session.query(ProdDiskSurvey)
            .filter(ProdDiskSurvey.inventory_name == inventory_name)
            .order_by(ProdDiskSurvey.surveyed_at.desc())
            .limit(limit)
            .all()
        )

    def record_service_audit(
        self,
        inventory_name: str,
        host: str,
        service_name: str,
        action: ProdServiceAuditAction,
        occurred_at: datetime,
        size_bytes: int | None = None,
        note: str | None = None,
    ) -> ProdServiceAudit:
        row = ProdServiceAudit(
            inventory_name=inventory_name,
            host=host,
            service_name=service_name,
            action=action,
            occurred_at=occurred_at,
            size_bytes=size_bytes,
            note=note,
        )
        self.session.add(row)
        self.session.commit()
        return row

    def list_service_audit(
        self,
        inventory_name: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[ProdServiceAudit]:
        query = self.session.query(ProdServiceAudit).filter(
            ProdServiceAudit.inventory_name == inventory_name
        )
        if since is not None:
            query = query.filter(ProdServiceAudit.occurred_at >= since)
        if until is not None:
            query = query.filter(ProdServiceAudit.occurred_at <= until)
        return query.order_by(ProdServiceAudit.occurred_at.asc()).all()


#: Operator-facing availability states for a pooled file. These map onto claim
#: status, not onto columns of ``prod_file_pool``: a file is AVAILABLE when it
#: carries no blocking claim, CLAIMED while one is open, and UPLOADED once that
#: claim resolves. Keeping CLAIMED distinct from UPLOADED matters in both
#: directions — a forgotten desktop checkout must not read as "on the network",
#: and a released claim must not read as spent forever.
POOL_STATUS_AVAILABLE = "available"
POOL_STATUS_CLAIMED = "claimed"
POOL_STATUS_UPLOADED = "uploaded"
#: Not a fourth *availability* state — a partially-uploaded file IS available —
#: but a filter operators need, because those files have chunks in production
#: already and are the ones worth completing.
POOL_STATUS_PARTIAL = "partial"
POOL_STATUSES = (
    POOL_STATUS_AVAILABLE,
    POOL_STATUS_CLAIMED,
    POOL_STATUS_UPLOADED,
    POOL_STATUS_PARTIAL,
)


def locator_digest(locator: str) -> str:
    """SHA256 of a locator, used as its uniqueness key.

    Uniqueness is enforced on this rather than on ``locator`` itself: a btree
    unique index over a long URL can exceed Postgres' per-entry size limit, and
    a fixed 64-char index is materially smaller across the hundreds of
    thousands of rows a local dataset contributes.
    """
    return hashlib.sha256(locator.encode("utf-8")).hexdigest()

class InsufficientPoolInventory(Exception):
    """Raised when the pool cannot satisfy a size request.

    Carries the shortfall per size class so the caller can tell the operator
    exactly what to top up. Deliberately an exception rather than a partial
    result: a canary asked for 350 files and given 200 would silently produce a
    smaller experiment than the one that was designed.
    """

    def __init__(self, kind: PoolFileKind, shortfall: dict):
        self.kind = kind
        #: {size_class_value: {"requested": int, "available": int}}
        self.shortfall = shortfall
        parts = ", ".join(
            f"{cls} {detail['requested']} requested / {detail['available']} available"
            for cls, detail in sorted(shortfall.items())
        )
        super().__init__(
            f"the {kind.value} pool cannot satisfy the request: {parts}"
        )


class ProdFilePoolRepository(ABC):
    """The pool of real files available for uploading to production Autonomi.

    Standalone (no ``deployment_id`` on the catalogue), like
    ``ProdFleetRepository``: the pool outlives any single run, is populated and
    inspected independently of deployments, and serves a GUI desktop
    application that has no link to this database at all.
    """

    @abstractmethod
    def add_pool_file(
        self,
        kind: PoolFileKind,
        locator: str,
        name: str,
        size_bytes: int | None = None,
        digests: dict[str, str] | None = None,
        tags: list[str] | None = None,
        provenance: str | None = None,
        notes: str | None = None,
    ) -> tuple[ProdFilePool, bool]:
        """Catalogue a file, returning ``(file, created)``.

        Re-adding a known locator is a no-op returning ``created=False``, so
        re-running an acquisition over an overlapping listing is safe.

        When any supplied digest matches an existing file, the new row is
        linked to it via ``duplicate_of`` instead of becoming a rival entry.
        That is what stops an Internet Archive URL and a local copy of the same
        content from both being handed out: consuming either spends both.
        """
        ...

    @abstractmethod
    def add_pool_files_bulk(self, entries: list[dict], batch_size: int = 500) -> dict:
        """Catalogue many files with a handful of statements per batch.

        :meth:`add_pool_file` holds a separate conversation with the database
        per file — two SELECTs, two INSERTs and a COMMIT. Against a managed
        Postgres that is ~160ms of round-trips to do ~1ms of work, so a
        directory of a few thousand files takes a quarter of an hour of waiting.

        Each entry is a dict of ``kind``, ``locator``, ``name`` and optionally
        ``size_bytes``, ``digests``, ``tags``, ``provenance``, ``notes``.

        Returns ``{"created": [...], "skipped": int, "linked": [(name, id)]}``.
        Semantics match the per-file path exactly, including duplicate linking
        by shared digest — which must also work *within* a batch, since two
        copies of one file can arrive in the same chunk.
        """
        ...

    @abstractmethod
    def get_pool_file(self, file_id: int) -> ProdFilePool | None:
        ...

    @abstractmethod
    def get_pool_file_by_locator(self, locator: str) -> ProdFilePool | None:
        ...

    @abstractmethod
    def find_pool_file_by_digests(
        self, digests: dict[str, str]
    ) -> ProdFilePool | None:
        """The first catalogued file sharing any (algorithm, value) supplied.

        Matching on *any* shared algorithm is the point: the Internet Archive
        publishes MD5 and SHA1, a local file is hashed with SHA1 and SHA256, and
        the two can only ever meet on the algorithm they have in common.
        """
        ...

    @abstractmethod
    def add_pool_file_digests(self, file_id: int, digests: dict[str, str]) -> None:
        """Attach digests to a file, ignoring any algorithm it already carries."""
        ...

    @abstractmethod
    def update_pool_file_size(self, file_id: int, size_bytes: int | None) -> None:
        ...

    @abstractmethod
    def set_pool_file_active(self, file_id: int, active: bool) -> None:
        """Retire (or restore) an entry — a URL that 404s, a path that is gone.

        Deactivation rather than deletion, because a retired file may still
        carry the claim proving its content is on the network.
        """
        ...

    @abstractmethod
    def canonical_pool_file_id(self, file_id: int) -> int:
        """The id owning this file's content: itself, or what it duplicates."""
        ...

    @abstractmethod
    def list_pool_files(
        self,
        kind: PoolFileKind | None = None,
        size_class: SizeClass | None = None,
        status: str | None = None,
        tags: list[str] | None = None,
        active: bool | None = True,
        limit: int | None = None,
        added_since: datetime | None = None,
        exclude_partial: bool = False,
    ) -> list[ProdFilePool]:
        ...

    @abstractmethod
    def pool_stats(self) -> dict:
        """Availability counts per (kind, size class), plus unknown-size totals.

        Reported per kind because the two kinds are separate inventories: a
        remote uploader cannot use a local path, so "4 medium available" is
        meaningless without saying which pool it is in.
        """
        ...


    @abstractmethod
    def claim_pool_files(
        self,
        kind: PoolFileKind,
        request: dict,
        source: ClaimSource,
        label: str | None = None,
        deployment_id: int | None = None,
        deployment_name: str | None = None,
        when: datetime | None = None,
        exclude_partial: bool = False,
    ) -> list[ProdFilePoolClaim]:
        """Claim files matching a {SizeClass: count} request, atomically.

        Raises :class:`InsufficientPoolInventory` if any class cannot be
        satisfied, having claimed nothing — a half-filled request would produce
        a smaller experiment than the one designed.

        Concurrency is arbitrated by the database, not by this method: the
        partial unique index refuses a second blocking claim, so two runs racing
        for the last large file cannot both win however they interleave. A loser
        moves on to the next candidate rather than failing.

        ``exclude_partial`` skips files carrying a PARTIAL claim. Set by canary
        selection: those files already have chunks in production, possibly
        months old, so a loss measurement over them would be measuring something
        other than what it reports.
        """
        ...

    @abstractmethod
    def list_pool_claims(
        self,
        status: ClaimStatus | None = None,
        source: ClaimSource | None = None,
        label: str | None = None,
        stale_before: datetime | None = None,
        unresolvable: bool = False,
        deployment_id: int | None = None,
        file_id: int | None = None,
    ) -> list[ProdFilePoolClaim]:
        """Claims matching the filters, newest first.

        ``unresolvable`` narrows to open claims that no ``record-results`` run
        can ever close — the deployment row is gone, or the deployment was
        destroyed, which means its InfluxDB went with it. Those need the
        operator, and surfacing them is the difference between a wedged claim
        and a known one.
        """
        ...

    @abstractmethod
    def open_claim_for_file(self, file_id: int) -> ProdFilePoolClaim | None:
        """The blocking claim on this file's content, if any."""
        ...

    @abstractmethod
    def resolve_pool_claim(
        self,
        claim_id: int,
        address: str | None = None,
        chunks_stored: int | None = None,
        chunks_failed: int | None = None,
        total_chunks: int | None = None,
        storage_cost_atto: Decimal | None = None,
        gas_cost_wei: Decimal | None = None,
        locator_used: str | None = None,
        note: str | None = None,
        when: datetime | None = None,
    ) -> ProdFilePoolClaim:
        """Mark a claim uploaded, recording what the network gave back.

        An UPSERT rather than a one-shot: ``record-results`` is idempotent and
        re-runnable, and a later run can revise costs. Re-resolving an already
        resolved claim updates it in place.
        """
        ...

    @abstractmethod
    def mark_pool_claim_partial(
        self,
        claim_id: int,
        address: str | None = None,
        chunks_stored: int | None = None,
        chunks_failed: int | None = None,
        total_chunks: int | None = None,
        note: str | None = None,
        when: datetime | None = None,
    ) -> ProdFilePoolClaim:
        """Record that an upload stored some chunks and stopped.

        The file returns to the pool — it is not on the network in full — but
        the chunk counts are kept, so an operator can see how far it got rather
        than being told only that it failed.
        """
        ...

    @abstractmethod
    def release_pool_claim(
        self,
        claim_id: int,
        note: str | None = None,
        when: datetime | None = None,
    ) -> ProdFilePoolClaim:
        """Give a claimed file back to the pool. Never automatic — a claim that
        might correspond to a real upload must not be released by a sweep."""
        ...

    @abstractmethod
    def blocking_claims_for_files(self, file_ids: list[int]) -> dict:
        """Map file id → its blocking claim, for a batch of files.

        Batched rather than per-row because a listing renders each file's state
        alongside it, and one query per row would make `pool list` quadratic on
        a catalogue holding hundreds of thousands of entries. Duplicate rows are
        resolved to their canonical content, so a mirror reports the claim taken
        through its counterpart.
        """
        ...

    @abstractmethod
    def files_with_partial_claims(self, file_ids: list[int]) -> set:
        """Which of these files carry a PARTIAL claim.

        Batched alongside :meth:`blocking_claims_for_files` because a listing
        needs both to describe a row: a partial claim does NOT block, so it is
        invisible to the blocking lookup, and a file that has had chunks stored
        would otherwise render identically to one nobody has ever touched.
        """
        ...

    @abstractmethod
    def latest_partial_claim_for_file(self, file_id: int) -> ProdFilePoolClaim | None:
        """The most recent PARTIAL claim on this file's content, if any."""
        ...

    @abstractmethod
    def get_pool_file_by_address(self, address: str) -> ProdFilePool | None:
        """Reverse lookup: which pooled file produced this datamap address?

        Answerable only because the address is recorded on the claim when it
        resolves; there is no other route from an address back to its content.
        """
        ...

    @abstractmethod
    def seed_uploaded_claim(
        self,
        file_id: int,
        source: ClaimSource,
        status: ClaimStatus = ClaimStatus.UPLOADED,
        address: str | None = None,
        chunks_stored: int | None = None,
        chunks_failed: int | None = None,
        total_chunks: int | None = None,
        storage_cost_atto: Decimal | None = None,
        gas_cost_wei: Decimal | None = None,
        deployment_id: int | None = None,
        deployment_name: str | None = None,
        when: datetime | None = None,
        note: str | None = None,
    ) -> ProdFilePoolClaim | None:
        """Record a file as already uploaded, in one step.

        Distinct from claim-then-resolve: this is for content whose upload
        already happened elsewhere — historical results being imported, or an
        upload made outside the pool. Returns None if the file already carries a
        blocking claim, so re-running is safe.
        """
        ...

    @abstractmethod
    def attach_deployment_to_claims(
        self, claim_ids: list[int], deployment_id: int, deployment_name: str
    ) -> None:
        """Backfill the deployment on claims taken before its row existed.

        Selection has to happen before any infrastructure is created, but
        ``record_deployment_created`` always inserts a *fresh* row — so the id
        cannot be known at claim time and must be attached afterwards.
        """
        ...


class SqlAlchemyProdFilePoolRepository(ProdFilePoolRepository):
    def __init__(self, session):
        self.session = session

    # ---------------------------------------------------------------- helpers

    def _canonical_id_expr(self):
        """SQL for "the id owning this row's content".

        Duplicate links are kept flat by :meth:`add_pool_file` — a duplicate
        always points at a canonical row, never at another duplicate — so one
        COALESCE resolves the group without a recursive walk.
        """
        return func.coalesce(ProdFilePool.duplicate_of, ProdFilePool.id)

    def _blocking_claim_exists(self):
        """EXISTS predicate: this row's content already carries a claim that
        makes it unusable (open, or already uploaded).

        Anchored on the *canonical* id so a duplicate entry is blocked by a
        claim taken against the content through its other locator.
        """
        return (
            select(ProdFilePoolClaim.id)
            .where(
                ProdFilePoolClaim.file_id == self._canonical_id_expr(),
                ProdFilePoolClaim.status.notin_(tuple(NON_BLOCKING_STATUSES)),
            )
            .exists()
        )

    def _status_filter(self, query, status: str | None):
        if status is None:
            return query
        if status == POOL_STATUS_AVAILABLE:
            return query.filter(~self._blocking_claim_exists())
        if status in (POOL_STATUS_CLAIMED, POOL_STATUS_UPLOADED, POOL_STATUS_PARTIAL):
            wanted = {
                POOL_STATUS_CLAIMED: ClaimStatus.CLAIMED,
                POOL_STATUS_UPLOADED: ClaimStatus.UPLOADED,
                POOL_STATUS_PARTIAL: ClaimStatus.PARTIAL,
            }[status]
            return query.filter(
                select(ProdFilePoolClaim.id)
                .where(
                    ProdFilePoolClaim.file_id == self._canonical_id_expr(),
                    ProdFilePoolClaim.status == wanted,
                )
                .exists()
            )
        raise ValueError(
            f"unknown pool status '{status}'. Valid: {', '.join(POOL_STATUSES)}"
        )

    # ------------------------------------------------------------- catalogue

    def add_pool_file(
        self,
        kind: PoolFileKind,
        locator: str,
        name: str,
        size_bytes: int | None = None,
        digests: dict[str, str] | None = None,
        tags: list[str] | None = None,
        provenance: str | None = None,
        notes: str | None = None,
    ) -> tuple[ProdFilePool, bool]:
        existing = self.get_pool_file_by_locator(locator)
        if existing is not None:
            # Re-adding a known locator refreshes what we have learned about it
            # (a size resolved on a later pass, extra digests) without creating
            # a rival row or disturbing its claims.
            if size_bytes is not None and existing.size_bytes is None:
                existing.size_bytes = size_bytes
            if digests:
                self.add_pool_file_digests(existing.id, digests)
            if tags:
                self.add_pool_file_tags(existing.id, tags)
            self.session.commit()
            return existing, False

        duplicate_of = None
        if digests:
            match = self.find_pool_file_by_digests(digests)
            if match is not None:
                # Flatten: point at the canonical row, never at another
                # duplicate, so availability resolves in one COALESCE.
                duplicate_of = match.duplicate_of or match.id

        pool_file = ProdFilePool(
            kind=kind,
            locator=locator,
            locator_sha256=locator_digest(locator),
            name=name,
            size_bytes=size_bytes,
            provenance=provenance,
            notes=notes,
            duplicate_of=duplicate_of,
        )
        self.session.add(pool_file)
        self.session.flush()
        if digests:
            self._add_digests_no_commit(pool_file.id, digests, set())
        if tags:
            self._add_tags_no_commit(pool_file.id, tags, set())
        self.session.commit()
        return pool_file, True

    def add_pool_files_bulk(self, entries: list[dict], batch_size: int = 500) -> dict:
        created: list = []
        linked: list = []
        skipped = 0
        for start in range(0, len(entries), batch_size):
            chunk = entries[start : start + batch_size]
            made, link, skip = self._add_pool_files_chunk(chunk)
            created.extend(made)
            linked.extend(link)
            skipped += skip
        return {"created": created, "skipped": skipped, "linked": linked}

    def _add_pool_files_chunk(self, chunk: list[dict]):
        """One batch: a fixed handful of statements, not a handful per file.

        Core inserts rather than ORM ``session.add`` per row — the ORM issues an
        INSERT..RETURNING per object to populate its identity, which is exactly
        the per-row round-trip this method exists to avoid. Measured on 50
        files: 252 statements the ORM way, 8 this way.
        """
        prepared = []
        for entry in chunk:
            digests = {
                str(algo).lower().strip(): str(value).lower().strip()
                for algo, value in (entry.get("digests") or {}).items()
                if algo and value
            }
            tags = sorted(
                {
                    str(value).lower().strip()
                    for value in (entry.get("tags") or [])
                    if str(value).strip()
                }
            )
            prepared.append(
                {
                    "entry": entry,
                    "hash": locator_digest(entry["locator"]),
                    "digests": digests,
                    "tags": tags,
                }
            )

        # (1) Which of these do we already have?
        existing_rows = (
            self.session.query(ProdFilePool)
            .filter(
                ProdFilePool.locator_sha256.in_([p["hash"] for p in prepared])
            )
            .all()
        )
        existing_by_hash = {row.locator_sha256: row for row in existing_rows}
        # What those rows already carry, in two queries rather than a lazy load
        # per row — otherwise re-running a section costs as much as the first
        # run, which is exactly the case an interrupted import hits.
        existing_algos: dict = {}
        existing_tags: dict = {}
        if existing_rows:
            existing_ids = [row.id for row in existing_rows]
            for row in (
                self.session.query(ProdFilePoolDigest)
                .filter(ProdFilePoolDigest.file_id.in_(existing_ids))
                .all()
            ):
                existing_algos.setdefault(row.file_id, set()).add(row.algo)
            for row in (
                self.session.query(ProdFilePoolTag)
                .filter(ProdFilePoolTag.file_id.in_(existing_ids))
                .all()
            ):
                existing_tags.setdefault(row.file_id, set()).add(row.tag)

        # (2) Which files already carry any of these digests?
        all_values = {v for p in prepared for v in p["digests"].values()}
        digest_owner: dict = {}
        if all_values:
            for row in (
                self.session.query(ProdFilePoolDigest)
                .filter(ProdFilePoolDigest.value.in_(list(all_values)))
                .all()
            ):
                digest_owner.setdefault((row.algo, row.value), row.file_id)

        # (3) Flatten those owners to their canonical rows in one go, so a
        # duplicate never points at another duplicate.
        canonical_of: dict = {}
        if digest_owner:
            for row in (
                self.session.query(ProdFilePool)
                .filter(ProdFilePool.id.in_(set(digest_owner.values())))
                .all()
            ):
                canonical_of[row.id] = row.duplicate_of or row.id

        skipped = 0
        new_files: list[dict] = []
        extra_digests: list[dict] = []
        extra_tags: list[dict] = []
        # Digests claimed by an earlier entry in THIS batch, keyed to its
        # locator hash — ids do not exist yet, so links are resolved by locator
        # and rewritten once the insert has assigned them.
        pending_owner: dict = {}
        link_by_hash: dict = {}

        for item in prepared:
            existing = existing_by_hash.get(item["hash"])
            if existing is not None:
                skipped += 1
                # Preserve the per-file path's merge behaviour: a re-run adding
                # a tag, or a later pass that resolved a previously unknown
                # size, must still take effect.
                entry = item["entry"]
                if entry.get("size_bytes") is not None and existing.size_bytes is None:
                    existing.size_bytes = entry["size_bytes"]
                have_algos = existing_algos.get(existing.id, set())
                extra_digests.extend(
                    {"file_id": existing.id, "algo": algo, "value": value}
                    for algo, value in item["digests"].items()
                    if algo not in have_algos
                )
                have_tags = existing_tags.get(existing.id, set())
                extra_tags.extend(
                    {"file_id": existing.id, "tag": tag}
                    for tag in item["tags"]
                    if tag not in have_tags
                )
                continue

            owner_id = next(
                (
                    digest_owner[pair]
                    for pair in item["digests"].items()
                    if pair in digest_owner
                ),
                None,
            )
            if owner_id is not None:
                link_by_hash[item["hash"]] = canonical_of.get(owner_id, owner_id)
            else:
                # Two copies of one file can arrive in the same batch; the
                # query above could not see the first, because it is not
                # inserted yet.
                sibling = next(
                    (
                        pending_owner[pair]
                        for pair in item["digests"].items()
                        if pair in pending_owner
                    ),
                    None,
                )
                if sibling is not None:
                    link_by_hash[item["hash"]] = sibling
                else:
                    for pair in item["digests"].items():
                        pending_owner.setdefault(pair, item["hash"])

            entry = item["entry"]
            new_files.append(
                {
                    "kind": entry["kind"],
                    "locator": entry["locator"],
                    "locator_sha256": item["hash"],
                    "name": entry["name"],
                    "size_bytes": entry.get("size_bytes"),
                    "provenance": entry.get("provenance"),
                    "notes": entry.get("notes"),
                    "active": True,
                    "added_at": datetime.now(timezone.utc),
                }
            )

        created: list = []
        link_report: list = []
        if new_files:
            # (4) One bulk insert for the files.
            self.session.execute(insert(ProdFilePool), new_files)
            self.session.flush()
            # (5) One select to learn the ids that were assigned.
            created = (
                self.session.query(ProdFilePool)
                .filter(
                    ProdFilePool.locator_sha256.in_(
                        [f["locator_sha256"] for f in new_files]
                    )
                )
                .all()
            )
            id_by_hash = {row.locator_sha256: row.id for row in created}

            for item in prepared:
                file_id = id_by_hash.get(item["hash"])
                if file_id is None:
                    continue
                extra_digests.extend(
                    {"file_id": file_id, "algo": algo, "value": value}
                    for algo, value in item["digests"].items()
                )
                extra_tags.extend(
                    {"file_id": file_id, "tag": tag} for tag in item["tags"]
                )

            # (6) Resolve the within-batch links now that ids exist.
            links = []
            for locator_hash, target in link_by_hash.items():
                file_id = id_by_hash.get(locator_hash)
                if file_id is None:
                    continue
                resolved = id_by_hash.get(target, target) if isinstance(target, str) else target
                if resolved != file_id:
                    links.append({"row_id": file_id, "target": resolved})
            name_by_id = {row.id: row.name for row in created}
            for link in links:
                self.session.query(ProdFilePool).filter(
                    ProdFilePool.id == link["row_id"]
                ).update({"duplicate_of": link["target"]}, synchronize_session=False)
                link_report.append((name_by_id.get(link["row_id"], ""), link["target"]))

        # (7) and (8): one bulk insert each for digests and tags.
        if extra_digests:
            self.session.execute(insert(ProdFilePoolDigest), extra_digests)
        if extra_tags:
            self.session.execute(insert(ProdFilePoolTag), extra_tags)
        self.session.commit()
        # Deliberately no refresh loop here: it cost a SELECT per row (plus a
        # lazy tag load each), which was the bulk of the statements this method
        # exists to remove. The links are already known from the pass above.
        return created, link_report, skipped

    def get_pool_file(self, file_id: int) -> ProdFilePool | None:
        return self.session.get(ProdFilePool, file_id)

    def get_pool_file_by_locator(self, locator: str) -> ProdFilePool | None:
        return (
            self.session.query(ProdFilePool)
            .filter(ProdFilePool.locator_sha256 == locator_digest(locator))
            .one_or_none()
        )

    def find_pool_file_by_digests(
        self, digests: dict[str, str]
    ) -> ProdFilePool | None:
        if not digests:
            return None
        clauses = [
            (ProdFilePoolDigest.algo == algo.lower().strip())
            & (ProdFilePoolDigest.value == value.lower().strip())
            for algo, value in digests.items()
            if algo and value
        ]
        if not clauses:
            return None
        predicate = clauses[0]
        for clause in clauses[1:]:
            predicate = predicate | clause
        row = (
            self.session.query(ProdFilePoolDigest)
            .filter(predicate)
            .order_by(ProdFilePoolDigest.file_id)
            .first()
        )
        return self.session.get(ProdFilePool, row.file_id) if row else None

    def _add_digests_no_commit(
        self, file_id: int, digests: dict[str, str], existing_algos: set[str]
    ) -> None:
        for algo, value in digests.items():
            if not algo or not value:
                continue
            normalised = algo.lower().strip()
            if normalised in existing_algos:
                continue
            existing_algos.add(normalised)
            self.session.add(
                ProdFilePoolDigest(
                    file_id=file_id,
                    algo=normalised,
                    value=value.lower().strip(),
                )
            )

    def _add_tags_no_commit(self, file_id: int, tags, existing: set) -> None:
        for value in tags:
            tag = str(value).strip().lower()
            if not tag or tag in existing:
                continue
            existing.add(tag)
            self.session.add(ProdFilePoolTag(file_id=file_id, tag=tag))

    def add_pool_file_tags(self, file_id: int, tags) -> None:
        if not tags:
            return
        present = {
            row.tag
            for row in self.session.query(ProdFilePoolTag).filter(
                ProdFilePoolTag.file_id == file_id
            )
        }
        self._add_tags_no_commit(file_id, tags, present)
        self.session.commit()

    def add_pool_file_digests(self, file_id: int, digests: dict[str, str]) -> None:
        if not digests:
            return
        present = {
            row.algo
            for row in self.session.query(ProdFilePoolDigest).filter(
                ProdFilePoolDigest.file_id == file_id
            )
        }
        self._add_digests_no_commit(file_id, digests, present)
        self.session.commit()

    def update_pool_file_size(self, file_id: int, size_bytes: int | None) -> None:
        pool_file = self.session.get(ProdFilePool, file_id)
        if pool_file is None:
            return
        pool_file.size_bytes = size_bytes
        self.session.commit()

    def set_pool_file_active(self, file_id: int, active: bool) -> None:
        pool_file = self.session.get(ProdFilePool, file_id)
        if pool_file is None:
            return
        pool_file.active = active
        self.session.commit()

    def canonical_pool_file_id(self, file_id: int) -> int:
        pool_file = self.session.get(ProdFilePool, file_id)
        if pool_file is None:
            raise ValueError(f"no pool file with id {file_id}")
        return pool_file.duplicate_of or pool_file.id

    def list_pool_files(
        self,
        kind: PoolFileKind | None = None,
        size_class: SizeClass | None = None,
        status: str | None = None,
        tags: list[str] | None = None,
        active: bool | None = True,
        limit: int | None = None,
        added_since: datetime | None = None,
        exclude_partial: bool = False,
    ) -> list[ProdFilePool]:
        query = self.session.query(ProdFilePool)
        if exclude_partial:
            query = query.filter(~self._has_partial_claim())
        if added_since is not None:
            query = query.filter(ProdFilePool.added_at >= added_since)
        if kind is not None:
            query = query.filter(ProdFilePool.kind == kind)
        if active is not None:
            query = query.filter(ProdFilePool.active == active)
        if size_class is not None:
            lower, upper = byte_range(size_class)
            # Filters the indexed byte column rather than classifying per row —
            # the difference between an index range scan and a full scan of a
            # catalogue holding hundreds of thousands of local files.
            query = query.filter(ProdFilePool.size_bytes >= lower)
            if upper is not None:
                query = query.filter(ProdFilePool.size_bytes < upper)
        if tags:
            # In SQL, not in Python. Filtering after the query returned meant
            # materialising every candidate row to inspect it, and a LIMIT could
            # only be applied afterwards — 2.11s to return ten rows from a 200k
            # catalogue. Any of the given tags matches (OR), which is what the
            # Python version did.
            wanted = [str(value).strip().lower() for value in tags if str(value).strip()]
            if wanted:
                query = query.filter(
                    select(ProdFilePoolTag.id)
                    .where(
                        ProdFilePoolTag.file_id == ProdFilePool.id,
                        ProdFilePoolTag.tag.in_(wanted),
                    )
                    .exists()
                )
        query = self._status_filter(query, status)
        query = query.order_by(ProdFilePool.id)
        # LIMIT reaches the database. Slicing the result list instead loaded the
        # whole catalogue first, which is what made `list --limit 10` cost
        # seconds regardless of how few rows were wanted.
        if limit is not None:
            query = query.limit(limit)
        return query.all()

    def _count_pool_files(
        self,
        kind: PoolFileKind,
        size_class: SizeClass,
        available_only: bool = False,
    ) -> int:
        """COUNT in the database rather than len() over materialised rows.

        The previous implementation called ``list_pool_files`` twice per (kind,
        size class) — twenty full scans, measured at 4.47s on a 200k catalogue,
        to produce twenty integers.
        """
        lower, upper = byte_range(size_class)
        query = self.session.query(func.count(ProdFilePool.id)).filter(
            ProdFilePool.kind == kind,
            ProdFilePool.active.is_(True),
            ProdFilePool.size_bytes >= lower,
        )
        if upper is not None:
            query = query.filter(ProdFilePool.size_bytes < upper)
        if available_only:
            query = query.filter(~self._blocking_claim_exists())
        return int(query.scalar() or 0)

    def pool_stats(self) -> dict:
        stats: dict = {}
        for kind in PoolFileKind:
            per_class: dict[str, dict[str, int]] = {}
            for size_class in size_class_ordered():
                per_class[size_class.value] = {
                    "available": self._count_pool_files(
                        kind, size_class, available_only=True
                    ),
                    "total": self._count_pool_files(kind, size_class),
                }
            # Entries whose size could not be resolved are invisible to every
            # size-class request, so they are counted separately rather than
            # inflating a class the selector will never draw them from.
            unknown = (
                self.session.query(func.count(ProdFilePool.id))
                .filter(
                    ProdFilePool.kind == kind,
                    ProdFilePool.active.is_(True),
                    ProdFilePool.size_bytes.is_(None),
                )
                .scalar()
            )
            stats[kind.value] = {"by_class": per_class, "unknown_size": int(unknown)}
        return stats

    # ------------------------------------------------------------ selection

    #: How many candidates to fetch per file needed. Selection takes the
    #: oldest-first window and shuffles *within* it, rather than ordering the
    #: whole filtered set randomly: `ORDER BY random()` sorts every matching row,
    #: which on a catalogue of hundreds of thousands of local files is a full
    #: scan per request. The overfetch also supplies the fallbacks needed when a
    #: concurrent run claims a candidate first.
    _CANDIDATE_OVERFETCH = 5
    _CANDIDATE_FLOOR = 32

    def _has_partial_claim(self):
        return (
            select(ProdFilePoolClaim.id)
            .where(
                ProdFilePoolClaim.file_id == self._canonical_id_expr(),
                ProdFilePoolClaim.status == ClaimStatus.PARTIAL,
            )
            .exists()
        )

    def _candidates(
        self,
        kind: PoolFileKind,
        size_class: SizeClass,
        needed: int,
        exclude_partial: bool = False,
    ) -> list[ProdFilePool]:
        window = max(needed * self._CANDIDATE_OVERFETCH, self._CANDIDATE_FLOOR)
        rows = self.list_pool_files(
            kind=kind,
            size_class=size_class,
            status=POOL_STATUS_AVAILABLE,
            active=True,
            limit=None if exclude_partial else window,
            exclude_partial=exclude_partial,
        )
        return rows[:window]

    def claim_pool_files(
        self,
        kind: PoolFileKind,
        request: dict,
        source: ClaimSource,
        label: str | None = None,
        deployment_id: int | None = None,
        deployment_name: str | None = None,
        when: datetime | None = None,
        exclude_partial: bool = False,
    ) -> list[ProdFilePoolClaim]:
        import random

        stamp = when or datetime.now(timezone.utc)

        # Check the whole request before claiming anything. Claiming class by
        # class would leave the earlier classes spent when a later one runs
        # short, quietly burning inventory on a run that then aborts.
        shortfall: dict = {}
        plan: dict = {}
        for size_class, count in request.items():
            if count <= 0:
                continue
            candidates = self._candidates(
                kind, size_class, count, exclude_partial=exclude_partial
            )
            if len(candidates) < count:
                available = len(
                    self.list_pool_files(
                        kind=kind,
                        size_class=size_class,
                        status=POOL_STATUS_AVAILABLE,
                        active=True,
                        exclude_partial=exclude_partial,
                    )
                )
                if available < count:
                    shortfall[size_class.value] = {
                        "requested": count,
                        "available": available,
                    }
                    continue
                candidates = self.list_pool_files(
                    kind=kind,
                    size_class=size_class,
                    status=POOL_STATUS_AVAILABLE,
                    active=True,
                    exclude_partial=exclude_partial,
                )
            random.shuffle(candidates)
            plan[size_class] = (candidates, count)

        if shortfall:
            raise InsufficientPoolInventory(kind, shortfall)

        claims: list[ProdFilePoolClaim] = []
        for size_class, (candidates, count) in plan.items():
            taken = 0
            for candidate in candidates:
                if taken == count:
                    break
                canonical = candidate.duplicate_of or candidate.id
                claim = ProdFilePoolClaim(
                    file_id=canonical,
                    source=source,
                    label=label,
                    deployment_id=deployment_id,
                    deployment_name=deployment_name,
                    status=ClaimStatus.CLAIMED,
                    locator_used=candidate.locator,
                    claimed_at=stamp,
                )
                # A savepoint per insert: the unique index is the arbiter, so a
                # candidate another run claimed first raises here and we simply
                # move to the next one instead of failing the whole request.
                nested = self.session.begin_nested()
                try:
                    self.session.add(claim)
                    self.session.flush()
                    nested.commit()
                except IntegrityError:
                    nested.rollback()
                    continue
                claims.append(claim)
                taken += 1
            if taken < count:
                # Lost too many races to fill the request. Nothing is committed
                # yet, so rolling back leaves the pool exactly as it was.
                self.session.rollback()
                raise InsufficientPoolInventory(
                    kind,
                    {
                        size_class.value: {
                            "requested": count,
                            "available": taken,
                        }
                    },
                )

        self.session.commit()
        return claims

    # --------------------------------------------------------------- claims

    def list_pool_claims(
        self,
        status: ClaimStatus | None = None,
        source: ClaimSource | None = None,
        label: str | None = None,
        stale_before: datetime | None = None,
        unresolvable: bool = False,
        deployment_id: int | None = None,
        file_id: int | None = None,
    ) -> list[ProdFilePoolClaim]:
        query = self.session.query(ProdFilePoolClaim)
        if status is not None:
            query = query.filter(ProdFilePoolClaim.status == status)
        if source is not None:
            query = query.filter(ProdFilePoolClaim.source == source)
        if label is not None:
            query = query.filter(ProdFilePoolClaim.label == label)
        if deployment_id is not None:
            query = query.filter(ProdFilePoolClaim.deployment_id == deployment_id)
        if file_id is not None:
            query = query.filter(ProdFilePoolClaim.file_id == file_id)
        if stale_before is not None:
            query = query.filter(ProdFilePoolClaim.claimed_at < stale_before)
        if unresolvable:
            # Open, expected to self-resolve, but nothing will ever resolve it:
            # the deployment row is gone, or the deployment was destroyed, which
            # took its InfluxDB — and with it the results — permanently.
            destroyed = select(Deployment.id).where(
                Deployment.status == DeploymentStatus.DESTROYED
            )
            query = query.filter(
                ProdFilePoolClaim.status == ClaimStatus.CLAIMED,
                ProdFilePoolClaim.source.in_(tuple(SELF_RESOLVING_SOURCES)),
                (ProdFilePoolClaim.deployment_id.is_(None))
                | (ProdFilePoolClaim.deployment_id.in_(destroyed)),
            )
        return query.order_by(ProdFilePoolClaim.claimed_at.desc()).all()

    def open_claim_for_file(self, file_id: int) -> ProdFilePoolClaim | None:
        pool_file = self.session.get(ProdFilePool, file_id)
        if pool_file is None:
            return None
        canonical = pool_file.duplicate_of or pool_file.id
        return (
            self.session.query(ProdFilePoolClaim)
            .filter(
                ProdFilePoolClaim.file_id == canonical,
                ProdFilePoolClaim.status.notin_(tuple(NON_BLOCKING_STATUSES)),
            )
            .one_or_none()
        )

    def resolve_pool_claim(
        self,
        claim_id: int,
        address: str | None = None,
        chunks_stored: int | None = None,
        chunks_failed: int | None = None,
        total_chunks: int | None = None,
        storage_cost_atto: Decimal | None = None,
        gas_cost_wei: Decimal | None = None,
        locator_used: str | None = None,
        note: str | None = None,
        when: datetime | None = None,
    ) -> ProdFilePoolClaim:
        claim = self.session.get(ProdFilePoolClaim, claim_id)
        if claim is None:
            raise ValueError(f"no pool claim with id {claim_id}")
        if claim.status is ClaimStatus.RELEASED:
            raise ValueError(
                f"claim {claim_id} was released and cannot be resolved as uploaded"
            )
        claim.status = ClaimStatus.UPLOADED
        claim.resolved_at = when or datetime.now(timezone.utc)
        # Only overwrite with something. A re-run of record-results that has
        # lost a field must not blank what an earlier run captured.
        if address is not None:
            claim.address = address
        if chunks_stored is not None:
            claim.chunks_stored = chunks_stored
        if chunks_failed is not None:
            claim.chunks_failed = chunks_failed
        if total_chunks is not None:
            claim.total_chunks = total_chunks
        if storage_cost_atto is not None:
            claim.storage_cost_atto = storage_cost_atto
        if gas_cost_wei is not None:
            claim.gas_cost_wei = gas_cost_wei
        if locator_used is not None:
            claim.locator_used = locator_used
        if note is not None:
            claim.note = note
        self.session.commit()
        return claim

    def mark_pool_claim_partial(
        self,
        claim_id: int,
        address: str | None = None,
        chunks_stored: int | None = None,
        chunks_failed: int | None = None,
        total_chunks: int | None = None,
        note: str | None = None,
        when: datetime | None = None,
    ) -> ProdFilePoolClaim:
        claim = self.session.get(ProdFilePoolClaim, claim_id)
        if claim is None:
            raise ValueError(f"no pool claim with id {claim_id}")
        if claim.status is ClaimStatus.UPLOADED:
            raise ValueError(
                f"claim {claim_id} is already recorded as fully uploaded; "
                "downgrading it to partial would misreport what is on the network"
            )
        claim.status = ClaimStatus.PARTIAL
        claim.resolved_at = when or datetime.now(timezone.utc)
        # Same only-overwrite-with-something rule as resolve: a later pass that
        # has lost a field must not blank what an earlier one captured.
        if address is not None:
            claim.address = address
        if chunks_stored is not None:
            claim.chunks_stored = chunks_stored
        if chunks_failed is not None:
            claim.chunks_failed = chunks_failed
        if total_chunks is not None:
            claim.total_chunks = total_chunks
        if note is not None:
            claim.note = note
        self.session.commit()
        return claim

    def release_pool_claim(
        self,
        claim_id: int,
        note: str | None = None,
        when: datetime | None = None,
    ) -> ProdFilePoolClaim:
        claim = self.session.get(ProdFilePoolClaim, claim_id)
        if claim is None:
            raise ValueError(f"no pool claim with id {claim_id}")
        if claim.status is ClaimStatus.UPLOADED:
            raise ValueError(
                f"claim {claim_id} is already uploaded; releasing it would return "
                "a file that is on the network to the pool"
            )
        claim.status = ClaimStatus.RELEASED
        claim.resolved_at = when or datetime.now(timezone.utc)
        if note is not None:
            claim.note = note
        self.session.commit()
        return claim

    def attach_deployment_to_claims(
        self, claim_ids: list[int], deployment_id: int, deployment_name: str
    ) -> None:
        if not claim_ids:
            return
        for claim in (
            self.session.query(ProdFilePoolClaim)
            .filter(ProdFilePoolClaim.id.in_(claim_ids))
            .all()
        ):
            claim.deployment_id = deployment_id
            claim.deployment_name = deployment_name
        self.session.commit()

    def blocking_claims_for_files(self, file_ids: list[int]) -> dict:
        if not file_ids:
            return {}
        files = (
            self.session.query(ProdFilePool)
            .filter(ProdFilePool.id.in_(file_ids))
            .all()
        )
        canonical_by_file = {f.id: (f.duplicate_of or f.id) for f in files}
        canonical_ids = set(canonical_by_file.values())
        claims = (
            self.session.query(ProdFilePoolClaim)
            .filter(
                ProdFilePoolClaim.file_id.in_(canonical_ids),
                ProdFilePoolClaim.status.notin_(tuple(NON_BLOCKING_STATUSES)),
            )
            .all()
        )
        by_canonical = {claim.file_id: claim for claim in claims}
        return {
            file_id: by_canonical[canonical]
            for file_id, canonical in canonical_by_file.items()
            if canonical in by_canonical
        }

    def files_with_partial_claims(self, file_ids: list[int]) -> set:
        if not file_ids:
            return set()
        files = (
            self.session.query(ProdFilePool)
            .filter(ProdFilePool.id.in_(file_ids))
            .all()
        )
        canonical_by_file = {f.id: (f.duplicate_of or f.id) for f in files}
        partial_canonicals = {
            row.file_id
            for row in self.session.query(ProdFilePoolClaim)
            .filter(
                ProdFilePoolClaim.file_id.in_(set(canonical_by_file.values())),
                ProdFilePoolClaim.status == ClaimStatus.PARTIAL,
            )
            .all()
        }
        return {
            file_id
            for file_id, canonical in canonical_by_file.items()
            if canonical in partial_canonicals
        }

    def latest_partial_claim_for_file(self, file_id: int) -> ProdFilePoolClaim | None:
        pool_file = self.session.get(ProdFilePool, file_id)
        if pool_file is None:
            return None
        canonical = pool_file.duplicate_of or pool_file.id
        return (
            self.session.query(ProdFilePoolClaim)
            .filter(
                ProdFilePoolClaim.file_id == canonical,
                ProdFilePoolClaim.status == ClaimStatus.PARTIAL,
            )
            .order_by(ProdFilePoolClaim.claimed_at.desc())
            .first()
        )

    def get_pool_file_by_address(self, address: str) -> ProdFilePool | None:
        claim = (
            self.session.query(ProdFilePoolClaim)
            .filter(ProdFilePoolClaim.address == address)
            .first()
        )
        if claim is None:
            return None
        return self.session.get(ProdFilePool, claim.file_id)

    def seed_uploaded_claim(
        self,
        file_id: int,
        source: ClaimSource,
        status: ClaimStatus = ClaimStatus.UPLOADED,
        address: str | None = None,
        chunks_stored: int | None = None,
        chunks_failed: int | None = None,
        total_chunks: int | None = None,
        storage_cost_atto: Decimal | None = None,
        gas_cost_wei: Decimal | None = None,
        deployment_id: int | None = None,
        deployment_name: str | None = None,
        when: datetime | None = None,
        note: str | None = None,
    ) -> ProdFilePoolClaim | None:
        pool_file = self.session.get(ProdFilePool, file_id)
        if pool_file is None:
            return None
        canonical = pool_file.duplicate_of or pool_file.id
        stamp = when or datetime.now(timezone.utc)
        claim = ProdFilePoolClaim(
            file_id=canonical,
            source=source,
            deployment_id=deployment_id,
            deployment_name=deployment_name,
            status=status,
            address=address,
            chunks_stored=chunks_stored,
            chunks_failed=chunks_failed,
            total_chunks=total_chunks,
            storage_cost_atto=storage_cost_atto,
            gas_cost_wei=gas_cost_wei,
            claimed_at=stamp,
            resolved_at=stamp,
            note=note,
        )
        nested = self.session.begin_nested()
        try:
            self.session.add(claim)
            self.session.flush()
            nested.commit()
        except IntegrityError:
            # Already spent or claimed — the partial unique index says so, which
            # is what makes re-running this safe.
            nested.rollback()
            return None
        self.session.commit()
        return claim
