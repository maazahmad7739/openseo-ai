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
    promoted_from_backlog: int = 0


class ApproveOut(BaseModel):
    recommendation_id: UUID
    status: str
    promoted_from_backlog: int = 0


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


# ---------------------------------------------------------------------------
# Off-page operator queue (plan/33 + plan/34)
# ---------------------------------------------------------------------------

class OffpageGateSummary(BaseModel):
    gate: str
    outcome: str
    reason: Optional[str] = None


class OffpageCapOut(BaseModel):
    """Promotional headroom for the account + community this draft targets.

    Mirrors the drafter's cap gate (v_offpage_promo_cap_usage) so the
    operator sees the same numbers the pipeline would refuse on.
    """
    account_handle: str
    account_promotional_cap_30d: int
    account_promotional_30d: int
    account_cap_reached: bool
    community_promotional_cap_30d: int
    community_promotional_30d: int
    community_cap_reached: bool


class OffpageQueueItem(BaseModel):
    draft_id: UUID
    version: int
    status: str
    approved_at: Optional[datetime] = None
    created_at: datetime
    recommendation_id: UUID
    site_id: UUID
    client_name: Optional[str] = None
    domain: Optional[str] = None
    community_key: Optional[str] = None
    platform: Optional[str] = None
    post_kind: Optional[str] = None
    thread_url: Optional[str] = None
    thread_title: Optional[str] = None
    thread_snippet: Optional[str] = None
    discovery_query: Optional[str] = None
    context_source: Optional[str] = None
    context_chars: Optional[int] = None
    reply_char_limit: Optional[int] = None
    post_char_limit: Optional[int] = None
    min_words: Optional[int] = None
    advice_md: Optional[str] = None
    mention_md: Optional[str] = None
    body_md: str
    post_type: str
    helpfulness_score: Optional[float] = None
    promo_density: Optional[float] = None
    brand_mention_count: Optional[int] = None
    link_count: Optional[int] = None
    gates: List[OffpageGateSummary] = []
    account_handle: Optional[str] = None
    caps: Optional[OffpageCapOut] = None
    operator_choice: Optional[str] = None


class OffpageQueueOut(BaseModel):
    site_id: UUID
    active_cap: int
    total_active: int
    total_pending: int
    showing: int
    items: List[OffpageQueueItem] = []


class OffpageChooseRequest(BaseModel):
    action: str = Field(..., description="'helpful' copies advice_md; "
                                        "'promotional' copies the full body "
                                        "and re-checks the 30d cap")


class OffpageChooseOut(BaseModel):
    draft_id: UUID
    status: str
    action: str
    copied_md: str
    mention_allowed: bool = False
    caps: Optional[OffpageCapOut] = None


class OffpageSkipRequest(BaseModel):
    reason: str = Field(min_length=1)


class OffpageSkipOut(BaseModel):
    draft_id: UUID
    status: str
    recommendation_id: UUID
    recommendation_status: str
    rejection_log_id: Optional[UUID] = None


class OffpagePermalinkRequest(BaseModel):
    permalink: str = Field(min_length=1)
    operator: Optional[str] = None


class OffpageCheckpointOut(BaseModel):
    checkpoint_index: int
    offset_minutes: int
    checkpoint_due_at: datetime


class OffpagePermalinkOut(BaseModel):
    publication_id: UUID
    draft_id: UUID
    site_id: UUID
    status: str
    post_type: str
    live_permalink: str
    submitted_at: datetime
    recommendation_status: str
    checkpoints: List[OffpageCheckpointOut] = []


# ---------------------------------------------------------------------------
# Off-page platform settings (plan/35 §3) - per-tenant channel control
# ---------------------------------------------------------------------------

class OffpagePlatformSettings(BaseModel):
    """One (site_id, platform) control row; exactly the fail-closed knobs
    the discovery job and the drafter gates read."""
    site_id: UUID
    platform: str
    enabled: bool = False
    discovery_seams: List[str] = []
    discovery_calls_per_day: int = 20
    reply_char_limit: Optional[int] = None
    post_char_limit: Optional[int] = None
    min_words: Optional[int] = None
    promotional_posts_per_30d: Optional[int] = None
    targeting: dict = {}


class OffpagePlatformSettingsOut(BaseModel):
    site_id: UUID
    items: List[OffpagePlatformSettings] = []
    active_channels: int = 0