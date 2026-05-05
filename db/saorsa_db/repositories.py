from abc import ABC, abstractmethod
from datetime import datetime, timezone

from saorsa_db.models.deployment import Deployment, DeploymentEvent, DeploymentStatus
from saorsa_db.models.file_result import DeploymentFileResult, FileOpType
from saorsa_db.models.host_result import DeploymentHostResult
from saorsa_db.models.migration import (
    Migration,
    MigrationStatus,
    MigrationVm,
    MigrationVmStatus,
)
from saorsa_db.models.prod_upload_result import (
    DeploymentProdUploadResult,
    ProdUploadStatus,
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
