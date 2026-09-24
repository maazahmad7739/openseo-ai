"""Pydantic response/request models (brief §1.1) — no bare dicts leak.

Field names match plan/05's recommendation schema exactly (brief §1.2).
"""

from datetime import date, datetime
from typing import Any, List, Optional
from uuid import UUID

from pydantic import BaseModel, Field


class RecommendationOut(BaseModel):
    recommendation_id: UUID
    site_id: UUID
    generator: str
    action_type: str
    target_url: Optional[str] = None
    proposed_url: Optional[str] = None
    cluster_id: Optional[UUID] = None
    diagnosis: str
    impact: str
    confidence: str
    effort: str
    owner: str
    status: str
    assigned_to: Optional[str] = None
    result: Optional[str] = None
    search_volume: Optional[int] = None
    primary_keyword: Optional[str] = None
    commercial_value: Optional[str] = None
    evidence_json: Optional[Any] = None


class QueueOut(BaseModel):
    site_id: UUID
    total_proposed: int
    showing: int
    recommendations: List[RecommendationOut]


class QueueDetailOut(BaseModel):
    recommendation_id: UUID
    site_id: UUID
    generator: str
    action_type: str
    target_url: Optional[str] = None
    proposed_url: Optional[str] = None
    cluster_id: Optional[UUID] = None
    diagnosis: str
    evidence_json: Any = None
    work_required_json: Any = None
    impact: str
    confidence: str
    effort: str
    owner: str
    status: str
    enriched: bool = False
    assigned_to: Optional[str] = None
    result: Optional[str] = None
    measurement_metric: Optional[str] = None
    measurement_window_days: Optional[int] = None
    measurement_due_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    approved_at: Optional[datetime] = None
    implemented_at: Optional[datetime] = None
    measured_at: Optional[datetime] = None
    cluster: Optional[dict] = None
    priority_score: Optional[float] = None
    measurement_plan: Optional[dict] = None
    catalogue: Optional[dict] = None
    serp_context: Optional[list] = None


class RejectRequest(BaseModel):
    reason: str = Field(min_length=1, description="required; stored in rejection_log "
                                                     "and fed back as agent context")


class RejectOut(BaseModel):
    recommendation_id: UUID
    status: str
    rejection_log_id: UUID


class ApproveOut(BaseModel):
    recommendation_id: UUID
    status: str


class AssignRequest(BaseModel):
    owner: str = Field(min_length=1, description="content | engineering | SEO | team member name")


class AssignOut(BaseModel):
    recommendation_id: UUID
    status: str
    owner: str


class SnapshotOut(BaseModel):
    snapshot_type: str
    comparison_type: str
    control_group_key: Optional[str] = None
    control_group_json: Optional[dict] = None
    period_start: date
    period_end: date
    impressions: Optional[int] = None
    avg_position: Optional[float] = None
    ctr: Optional[float] = None
    clicks: Optional[int] = None
    organic_sessions: Optional[int] = None
    orders: Optional[int] = None
    revenue: Optional[float] = None


class ClassificationOut(BaseModel):
    verdict: Optional[str] = None
    result: Optional[str] = None
    reasons: List[str] = []
    significance: Optional[dict] = None
    control_available: bool = False
    yoy_available: bool = False


class MeasurementOut(BaseModel):
    recommendation_id: UUID
    snapshots: List[SnapshotOut]
    classification: ClassificationOut


class ImplementRequest(BaseModel):
    implemented_at: Optional[date] = Field(
        None, description="implementation date; injectable per pipeline pattern")
    assigned_to: Optional[str] = None


class ImplementOut(BaseModel):
    recommendation_id: UUID
    status: str
    implemented_at: Optional[date] = None
    baseline: Optional[dict] = None


class MeasurementRunRequest(BaseModel):
    recommendation_ids: Optional[List[UUID]] = None
    reference_date: Optional[date] = Field(None, description="injectable clock; default today (UTC)")


class MeasurementRunOut(BaseModel):
    measured: List[dict]
    skipped: List[dict]


class ResultsOut(BaseModel):
    site_id: UUID
    total_measured: int
    win_rate: Optional[float] = None
    verdicts: dict = {}
    by_generator: List[dict] = []
    by_action_type: List[dict] = []
    recent: List[dict] = []
    incremental_clicks: int = 0
    incremental_revenue: float = 0
    measured_details: List[dict] = []


class StaleApprovalOut(BaseModel):
    recommendation_id: UUID
    site_id: UUID
    generator: str
    action_type: str
    diagnosis: str
    impact: str
    status: str
    approved_at: Optional[datetime] = None
    assigned_to: Optional[str] = None
    stale_days: int


class StaleApprovalsOut(BaseModel):
    stale_threshold_days: int
    items: List[StaleApprovalOut]


class PipelineItemOut(BaseModel):
    recommendation_id: UUID
    site_id: UUID
    generator: str
    action_type: str
    target_url: Optional[str] = None
    proposed_url: Optional[str] = None
    diagnosis: str
    impact: str
    status: str
    search_volume: Optional[int] = None
    primary_keyword: Optional[str] = None
    approved_at: Optional[datetime] = None
    implemented_at: Optional[datetime] = None
    assigned_to: Optional[str] = None
    observation_window_days: Optional[int] = None
    days_remaining: Optional[int] = None
    measurement_due_at: Optional[datetime] = None
    work_tasks: Optional[List[dict]] = None


class PipelineOut(BaseModel):
    site_id: UUID
    total_approved: int
    total_in_progress: int
    items: List[PipelineItemOut]


class HealthOut(BaseModel):
    status: str
    database: str
    integration_mode: str
    agent_credentials: str