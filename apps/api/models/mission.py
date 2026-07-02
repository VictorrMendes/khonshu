from __future__ import annotations

import enum
from datetime import datetime
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models.base import Base


def _enum_values(e):
    return [x.value for x in e]


class MissionStatus(enum.StrEnum):
    PENDING = "pending"
    PLANNING = "planning"
    WAITING_APPROVAL = "waiting_approval"
    READY = "ready"
    RUNNING = "running"
    PAUSED = "paused"
    RETRYING = "retrying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    # Valid transitions — enforced by MissionEngine, not the DB
    TRANSITIONS: dict  # type: ignore[assignment]


# Transition table — each status maps to the set of statuses it can move to
MissionStatus.TRANSITIONS = {  # type: ignore[attr-defined]
    MissionStatus.PENDING: {MissionStatus.PLANNING, MissionStatus.CANCELLED},
    MissionStatus.PLANNING: {
        MissionStatus.WAITING_APPROVAL,
        MissionStatus.READY,
        MissionStatus.FAILED,
    },
    MissionStatus.WAITING_APPROVAL: {
        MissionStatus.PLANNING,
        MissionStatus.READY,
        MissionStatus.CANCELLED,
    },
    MissionStatus.READY: {MissionStatus.RUNNING, MissionStatus.CANCELLED},
    MissionStatus.RUNNING: {
        MissionStatus.PAUSED,
        MissionStatus.RETRYING,
        MissionStatus.SUCCEEDED,
        MissionStatus.FAILED,
        MissionStatus.CANCELLED,
    },
    MissionStatus.PAUSED: {MissionStatus.RUNNING, MissionStatus.CANCELLED},
    MissionStatus.RETRYING: {MissionStatus.RUNNING, MissionStatus.FAILED},
    MissionStatus.SUCCEEDED: set(),
    MissionStatus.FAILED: set(),
    MissionStatus.CANCELLED: set(),
}


class MissionTrigger(enum.StrEnum):
    MANUAL = "manual"
    SCHEDULED = "scheduled"
    EVENT = "event"
    RULE = "rule"


class StepType(enum.StrEnum):
    TOOL = "tool"
    WORKFLOW = "workflow"
    AGENT = "agent"
    HUMAN = "human"


class StepStatus(enum.StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class FailurePolicy(enum.StrEnum):
    retry = "retry"    # retry up to max_retries (default)
    abort = "abort"    # fail the mission immediately
    skip = "skip"      # mark step skipped, continue
    # log the error, continue (alias for skip with different audit intent)
    ignore = "ignore"


class ExecutionPlanStatus(enum.StrEnum):
    DRAFT = "draft"
    VALIDATED = "validated"
    APPROVED = "approved"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SUPERSEDED = "superseded"


class ExecutionPlan(Base):
    """A specific strategy for executing a Mission.

    A Mission may have multiple ExecutionPlans across its lifetime
    (replanning after failure or rejection). Only one plan is active
    at a time; previous plans are retained with status=superseded.
    Human approval is attached to the specific plan, not the mission.
    See ADR-001.
    """

    __tablename__ = "execution_plans"

    id: Mapped[UUID] = mapped_column(
        sa.UUID(), primary_key=True, default=uuid4
    )
    mission_id: Mapped[UUID] = mapped_column(
        sa.UUID(),
        sa.ForeignKey("missions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(
        sa.Integer(), nullable=False, default=1
    )
    planner: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    status: Mapped[ExecutionPlanStatus] = mapped_column(
        sa.Enum(
            ExecutionPlanStatus,
            name="execution_plan_status",
            values_callable=_enum_values,
            create_type=False,
        ),
        nullable=False,
        default=ExecutionPlanStatus.DRAFT,
        index=True,
    )
    validation_errors: Mapped[list | None] = mapped_column(
        JSONB(), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )
    approved_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    approved_by: Mapped[str | None] = mapped_column(
        sa.Text(), nullable=True
    )

    mission: Mapped[Mission] = relationship(
        "Mission", back_populates="execution_plans"
    )
    steps: Mapped[list[MissionStep]] = relationship(
        "MissionStep",
        back_populates="execution_plan",
        cascade="all, delete-orphan",
        order_by="MissionStep.order",
        foreign_keys="MissionStep.execution_plan_id",
    )


class Mission(Base):
    __tablename__ = "missions"

    id: Mapped[UUID] = mapped_column(
        sa.UUID(), primary_key=True, default=uuid4
    )
    workspace_id: Mapped[UUID] = mapped_column(
        sa.UUID(),
        sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    intent: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    status: Mapped[MissionStatus] = mapped_column(
        sa.Enum(
            MissionStatus,
            name="mission_status",
            values_callable=_enum_values,
            create_type=False,
        ),
        nullable=False,
        default=MissionStatus.PENDING,
        index=True,
    )
    planner: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    executor: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    trigger: Mapped[MissionTrigger] = mapped_column(
        sa.Enum(
            MissionTrigger,
            name="mission_trigger",
            values_callable=_enum_values,
            create_type=False,
        ),
        nullable=False,
        default=MissionTrigger.MANUAL,
    )
    requires_approval: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, server_default=sa.false()
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )

    execution_plans: Mapped[list[ExecutionPlan]] = relationship(
        "ExecutionPlan",
        back_populates="mission",
        cascade="all, delete-orphan",
        order_by="ExecutionPlan.version",
    )
    steps: Mapped[list[MissionStep]] = relationship(
        "MissionStep",
        back_populates="mission",
        cascade="all, delete-orphan",
        order_by="MissionStep.order",
        foreign_keys="MissionStep.mission_id",
    )
    context: Mapped[MissionContext | None] = relationship(
        "MissionContext",
        back_populates="mission",
        uselist=False,
        cascade="all, delete-orphan",
    )
    artifacts: Mapped[list[MissionArtifact]] = relationship(
        "MissionArtifact",
        back_populates="mission",
        cascade="all, delete-orphan",
    )
    logs: Mapped[list[MissionLog]] = relationship(
        "MissionLog",
        back_populates="mission",
        cascade="all, delete-orphan",
    )
    # ADR-012: Execution is the canonical runtime unit; Mission aggregates
    # the Executions it spawned during planning. Lives in models/execution.py
    # (a separate, newer schema — see ADR-012 for the reconciliation plan).
    executions: Mapped[list["Execution"]] = relationship(
        "Execution",
        back_populates="mission",
        foreign_keys="Execution.mission_id",
    )

    def can_transition_to(self, target: MissionStatus) -> bool:
        allowed: set = MissionStatus.TRANSITIONS.get(  # type: ignore[attr-defined]
            self.status, set()
        )
        return target in allowed


class MissionStep(Base):
    __tablename__ = "mission_steps"

    id: Mapped[UUID] = mapped_column(
        sa.UUID(), primary_key=True, default=uuid4
    )
    mission_id: Mapped[UUID] = mapped_column(
        sa.UUID(),
        sa.ForeignKey("missions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    execution_plan_id: Mapped[UUID | None] = mapped_column(
        sa.UUID(),
        sa.ForeignKey("execution_plans.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    parent_step_id: Mapped[UUID | None] = mapped_column(
        sa.UUID(),
        sa.ForeignKey("mission_steps.id", ondelete="SET NULL"),
        nullable=True,
    )
    order: Mapped[int] = mapped_column(sa.Integer(), nullable=False)
    type: Mapped[StepType] = mapped_column(
        sa.Enum(
            StepType,
            name="step_type",
            values_callable=_enum_values,
            create_type=False,
        ),
        nullable=False,
        default=StepType.TOOL,
    )
    tool: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    input: Mapped[dict] = mapped_column(
        JSONB(), nullable=False, server_default=sa.text("'{}'")
    )
    output: Mapped[dict | None] = mapped_column(JSONB(), nullable=True)
    status: Mapped[StepStatus] = mapped_column(
        sa.Enum(
            StepStatus,
            name="step_status",
            values_callable=_enum_values,
            create_type=False,
        ),
        nullable=False,
        default=StepStatus.PENDING,
        index=True,
    )
    started_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    retry_count: Mapped[int] = mapped_column(
        sa.Integer(), nullable=False, server_default="0"
    )
    failure_policy: Mapped[FailurePolicy] = mapped_column(
        sa.Enum(
            FailurePolicy,
            name="failure_policy",
            values_callable=_enum_values,
            create_type=False,
        ),
        nullable=False,
        server_default="retry",
    )

    mission: Mapped[Mission] = relationship(
        "Mission",
        back_populates="steps",
        foreign_keys=[mission_id],
    )
    execution_plan: Mapped[ExecutionPlan | None] = relationship(
        "ExecutionPlan",
        back_populates="steps",
        foreign_keys=[execution_plan_id],
    )
    children: Mapped[list[MissionStep]] = relationship(
        "MissionStep",
        foreign_keys="MissionStep.parent_step_id",
    )


class MissionContext(Base):
    __tablename__ = "mission_contexts"

    id: Mapped[UUID] = mapped_column(
        sa.UUID(), primary_key=True, default=uuid4
    )
    mission_id: Mapped[UUID] = mapped_column(
        sa.UUID(),
        sa.ForeignKey("missions.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    conversation_id: Mapped[UUID | None] = mapped_column(
        sa.UUID(),
        sa.ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
    )
    event_id: Mapped[UUID | None] = mapped_column(
        sa.UUID(), nullable=True
    )
    available_capabilities: Mapped[list[str]] = mapped_column(
        ARRAY(sa.Text()), server_default="{}"
    )
    workspace_config: Mapped[dict] = mapped_column(
        JSONB(), nullable=False, server_default=sa.text("'{}'")
    )
    metadata_: Mapped[dict] = mapped_column(
        "metadata", JSONB(), nullable=False, server_default=sa.text("'{}'")
    )

    mission: Mapped[Mission] = relationship(
        "Mission", back_populates="context"
    )


class MissionArtifact(Base):
    __tablename__ = "mission_artifacts"

    id: Mapped[UUID] = mapped_column(
        sa.UUID(), primary_key=True, default=uuid4
    )
    mission_id: Mapped[UUID] = mapped_column(
        sa.UUID(),
        sa.ForeignKey("missions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    step_id: Mapped[UUID | None] = mapped_column(
        sa.UUID(),
        sa.ForeignKey("mission_steps.id", ondelete="SET NULL"),
        nullable=True,
    )
    # report | file | image | summary | log | patch | commit
    type: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    mime: Mapped[str] = mapped_column(
        sa.Text(),
        nullable=False,
        server_default="application/octet-stream",
    )
    name: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    uri: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    metadata_: Mapped[dict] = mapped_column(
        "metadata", JSONB(), nullable=False, server_default=sa.text("'{}'")
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )

    mission: Mapped[Mission] = relationship(
        "Mission", back_populates="artifacts"
    )


class MissionLog(Base):
    __tablename__ = "mission_logs"

    id: Mapped[UUID] = mapped_column(
        sa.UUID(), primary_key=True, default=uuid4
    )
    mission_id: Mapped[UUID] = mapped_column(
        sa.UUID(),
        sa.ForeignKey("missions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    step_id: Mapped[UUID | None] = mapped_column(
        sa.UUID(),
        sa.ForeignKey("mission_steps.id", ondelete="SET NULL"),
        nullable=True,
    )
    # info | warning | error
    level: Mapped[str] = mapped_column(
        sa.Text(), nullable=False, server_default="info"
    )
    message: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    metadata_: Mapped[dict] = mapped_column(
        "metadata", JSONB(), nullable=False, server_default=sa.text("'{}'")
    )
    occurred_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )

    mission: Mapped[Mission] = relationship(
        "Mission", back_populates="logs"
    )
