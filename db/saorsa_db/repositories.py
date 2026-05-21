from abc import ABC, abstractmethod
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from saorsa_db.models.deployment import Deployment, DeploymentEvent, DeploymentStatus
from saorsa_db.models.file_result import DeploymentFileResult, FileOpType
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
