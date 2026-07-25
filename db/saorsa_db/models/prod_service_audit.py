import enum
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from saorsa_db.db import Base


class ProdServiceAuditAction(enum.Enum):
    ADDED = "added"
    REMOVED = "removed"
    RESET = "reset"


class ProdServiceAudit(Base):
    """Audit trail of node-service lifecycle changes on a production host,
    written (best-effort) by ``prod services rm``/``add``/``reset-data``.

    Standalone and keyed by the inventory identifier string (e.g.
    ``ant-prod-01``) + host + service — production fleets have no ``Deployment``
    row. ``size_bytes`` is the reclaimed size on ``removed`` / the wiped size on
    ``reset`` (null for ``added``).
    """

    __tablename__ = "prod_service_audit"

    id: Mapped[int] = mapped_column(primary_key=True)
    inventory_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    host: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    service_name: Mapped[str] = mapped_column(String(64), nullable=False)

    action: Mapped[ProdServiceAuditAction] = mapped_column(
        Enum(ProdServiceAuditAction, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    size_bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    def to_dict(self):
        return {
            "id": self.id,
            "inventory_name": self.inventory_name,
            "host": self.host,
            "service_name": self.service_name,
            "action": self.action.value,
            "occurred_at": self.occurred_at.isoformat(),
            "size_bytes": self.size_bytes,
            "note": self.note,
            "recorded_at": self.recorded_at.isoformat(),
        }
