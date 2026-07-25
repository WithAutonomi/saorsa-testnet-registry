from abc import ABC, abstractmethod
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import delete, func, insert, select

from saorsa_db.models.deployment import Deployment, DeploymentEvent, DeploymentStatus
from saorsa_db.models.file_result import DeploymentFileResult, FileOpType
from saorsa_db.models.derived_metric import DeploymentDerivedMetric
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
from saorsa_db.models.prod_download_result import (
    DeploymentProdDownloadResult,
    ProdDownloadStatus,
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
