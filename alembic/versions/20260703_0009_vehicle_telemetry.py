"""таблицы GPS-телематики Stavtrack

Revision ID: 0009_vehicle_telemetry
Revises: 0008_distribution_centers
Create Date: 2026-07-03

Первый безопасный слой интеграции: храним сырые EGTS-пакеты от Stavtrack,
готовим таблицы для нормализованных GPS-точек и последнего состояния машины,
а также добавляем к машине ID объекта из Stavtrack.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009_vehicle_telemetry"
down_revision: Union[str, None] = "0008_distribution_centers"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("vehicles", sa.Column("stavtrack_object_id", sa.String(length=64), nullable=True))
    op.create_unique_constraint(
        "uq_vehicle_stavtrack_object",
        "vehicles",
        ["owner_id", "stavtrack_object_id"],
    )

    op.create_table(
        "vehicle_telemetry_raw_packets",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("protocol", sa.String(length=32), nullable=False, server_default="egts"),
        sa.Column("source", sa.String(length=64), nullable=False, server_default="stavtrack"),
        sa.Column("peer_host", sa.String(length=255), nullable=True),
        sa.Column("peer_port", sa.Integer(), nullable=True),
        sa.Column("terminal_id", sa.String(length=64), nullable=True),
        sa.Column(
            "vehicle_id",
            sa.Integer(),
            sa.ForeignKey("vehicles.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("payload", sa.LargeBinary(), nullable=False),
        sa.Column("payload_size", sa.Integer(), nullable=False),
        sa.Column("parse_status", sa.String(length=20), nullable=False, server_default="raw"),
        sa.Column("parse_error", sa.Text(), nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "parse_status IN ('raw','parsed','failed','ignored')",
            name="ck_telemetry_raw_parse_status",
        ),
    )
    op.create_index(
        "ix_vehicle_telemetry_raw_packets_terminal_id",
        "vehicle_telemetry_raw_packets",
        ["terminal_id"],
    )
    op.create_index(
        "ix_vehicle_telemetry_raw_packets_vehicle_id",
        "vehicle_telemetry_raw_packets",
        ["vehicle_id"],
    )
    op.create_index(
        "ix_vehicle_telemetry_raw_packets_received_at",
        "vehicle_telemetry_raw_packets",
        ["received_at"],
    )

    op.create_table(
        "vehicle_telemetry_points",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "raw_packet_id",
            sa.BigInteger(),
            sa.ForeignKey("vehicle_telemetry_raw_packets.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "owner_id",
            sa.Integer(),
            sa.ForeignKey("owners.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "vehicle_id",
            sa.Integer(),
            sa.ForeignKey("vehicles.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("terminal_id", sa.String(length=64), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("latitude", sa.Numeric(10, 7), nullable=True),
        sa.Column("longitude", sa.Numeric(10, 7), nullable=True),
        sa.Column("speed_kmh", sa.Numeric(7, 2), nullable=True),
        sa.Column("course", sa.Numeric(6, 2), nullable=True),
        sa.Column("ignition", sa.Boolean(), nullable=True),
        sa.Column("mileage_km", sa.Numeric(12, 3), nullable=True),
        sa.Column("is_valid", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("anomaly_reason", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=False, server_default="stavtrack"),
    )
    op.create_index("ix_vehicle_telemetry_points_raw_packet_id", "vehicle_telemetry_points", ["raw_packet_id"])
    op.create_index("ix_vehicle_telemetry_points_owner_id", "vehicle_telemetry_points", ["owner_id"])
    op.create_index("ix_vehicle_telemetry_points_vehicle_id", "vehicle_telemetry_points", ["vehicle_id"])
    op.create_index("ix_vehicle_telemetry_points_terminal_id", "vehicle_telemetry_points", ["terminal_id"])
    op.create_index("ix_vehicle_telemetry_points_observed_at", "vehicle_telemetry_points", ["observed_at"])

    op.create_table(
        "vehicle_states",
        sa.Column(
            "vehicle_id",
            sa.Integer(),
            sa.ForeignKey("vehicles.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("terminal_id", sa.String(length=64), nullable=True),
        sa.Column(
            "last_point_id",
            sa.BigInteger(),
            sa.ForeignKey("vehicle_telemetry_points.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("latitude", sa.Numeric(10, 7), nullable=True),
        sa.Column("longitude", sa.Numeric(10, 7), nullable=True),
        sa.Column("speed_kmh", sa.Numeric(7, 2), nullable=True),
        sa.Column("ignition", sa.Boolean(), nullable=True),
        sa.Column("is_valid", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("anomaly_reason", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_vehicle_states_terminal_id", "vehicle_states", ["terminal_id"])


def downgrade() -> None:
    op.drop_index("ix_vehicle_states_terminal_id", table_name="vehicle_states")
    op.drop_table("vehicle_states")

    op.drop_index("ix_vehicle_telemetry_points_observed_at", table_name="vehicle_telemetry_points")
    op.drop_index("ix_vehicle_telemetry_points_terminal_id", table_name="vehicle_telemetry_points")
    op.drop_index("ix_vehicle_telemetry_points_vehicle_id", table_name="vehicle_telemetry_points")
    op.drop_index("ix_vehicle_telemetry_points_owner_id", table_name="vehicle_telemetry_points")
    op.drop_index("ix_vehicle_telemetry_points_raw_packet_id", table_name="vehicle_telemetry_points")
    op.drop_table("vehicle_telemetry_points")

    op.drop_index("ix_vehicle_telemetry_raw_packets_received_at", table_name="vehicle_telemetry_raw_packets")
    op.drop_index("ix_vehicle_telemetry_raw_packets_vehicle_id", table_name="vehicle_telemetry_raw_packets")
    op.drop_index("ix_vehicle_telemetry_raw_packets_terminal_id", table_name="vehicle_telemetry_raw_packets")
    op.drop_table("vehicle_telemetry_raw_packets")

    op.drop_constraint("uq_vehicle_stavtrack_object", "vehicles", type_="unique")
    op.drop_column("vehicles", "stavtrack_object_id")
