from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional
import uuid


class PodState(Enum):
    RUNNING = "Running"
    PENDING = "Pending"
    CRASH_LOOP_BACK_OFF = "CrashLoopBackOff"
    IMAGE_PULL_BACK_OFF = "ImagePullBackOff"
    OOM_KILLED = "OOMKilled"
    ERROR = "Error"
    UNKNOWN = "Unknown"


class RemediationAction(Enum):
    INCREASE_MEMORY = "increase_memory"
    RESTART_POD = "restart_pod"
    ROLLBACK_DEPLOYMENT = "rollback_deployment"
    FIX_IMAGE_REFERENCE = "fix_image_reference"
    SCALE_NODE_GROUP = "scale_node_group"
    DELETE_AND_RECREATE = "delete_and_recreate"
    NO_ACTION = "no_action"


@dataclass
class PodIssue:
    pod_name: str
    namespace: str
    node_name: Optional[str]
    state: PodState
    reason: str
    message: str
    container_name: Optional[str]
    restart_count: int
    owner_kind: Optional[str]
    owner_name: Optional[str]
    logs_tail: Optional[str]
    events: list
    resource_requests: dict
    resource_limits: dict
    detected_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class AnalysisResult:
    issue: PodIssue
    root_cause: str
    recommended_action: RemediationAction
    confidence: float
    explanation: str
    prevention_steps: list
    raw_ai_response: str


@dataclass
class RemediationReport:
    issue: PodIssue
    analysis: AnalysisResult
    action_taken: RemediationAction
    success: bool
    details: str
    timestamp: datetime = field(default_factory=datetime.utcnow)
    report_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
