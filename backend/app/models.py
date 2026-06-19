"""
Pydantic models — request/response shapes for all V2 API routes.
Every route imports from here. Keep all data shapes in one place.
"""

from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime


# ── Onboarding ──────────────────────────────────────────────

class OnboardingInput(BaseModel):
    website_url: str
    linkedin_url: Optional[str] = ""
    service_description: str       # "we do performance marketing for SaaS"
    target_description: str        # "Series A-C, 20-200 employees"


# ── ICP ─────────────────────────────────────────────────────

class ICPOption(BaseModel):
    """One generated ICP suggestion shown to user during onboarding."""
    label: str                     # "Broad ICP" / "Niche ICP" / "Signal-Based ICP"
    icp_text: str                  # Full ICP.md text
    summary: str                   # 1-line summary shown in UI card


class ICPApproval(BaseModel):
    """User picks one of the 3 options or submits a custom one."""
    profile_id: str
    chosen_icp_text: str           # final approved ICP text
    user_context_text: str         # final approved UserContext text


class ICPChatMessage(BaseModel):
    """Message in the ICP building chatbot."""
    profile_id: str
    message: str
    history: List[dict] = []


# ── Profiles ────────────────────────────────────────────────

class ProfileCreate(BaseModel):
    user_id: str
    name: str                      # "Paid Social Agency"
    website_url: str
    linkedin_url: Optional[str] = ""
    service_description: str
    target_description: str


class ProfileUpdate(BaseModel):
    name: Optional[str] = None
    service_description: Optional[str] = None
    target_description: Optional[str] = None
    icp_text: Optional[str] = None


class ProfileResponse(BaseModel):
    id: str
    name: str
    service_description: Optional[str]
    icp_text: Optional[str]
    is_active: bool
    created_at: datetime


# ── Agents ──────────────────────────────────────────────────

class AgentTriggerRequest(BaseModel):
    """Manual trigger — user hits Run Now button."""
    profile_id: str                # which profile to run agents for
    agent_names: Optional[List[str]] = None  # None = all agents


class AgentRunStatus(BaseModel):
    agent_name: str
    status: str
    signals_found: int
    started_at: datetime
    completed_at: Optional[datetime]
    error_message: Optional[str]


# ── Leads ───────────────────────────────────────────────────

class LeadResponse(BaseModel):
    id: str
    company_name: Optional[str]
    company_url: Optional[str]
    signal_type: str
    why_flagged: Optional[str]
    intent_score: Optional[float]
    decision_maker: Optional[str]
    title: Optional[str]
    email: Optional[str]
    phone: Optional[str]
    linkedin_url: Optional[str]
    outreach_line: Optional[str]
    source_url: Optional[str]
    signal_date: Optional[datetime]
    status: str


class LeadStatusUpdate(BaseModel):
    status: str                    # viewed / saved / dismissed


# ── Signals ─────────────────────────────────────────────────

class RawSignal(BaseModel):
    """Internal model — what each agent produces."""
    signal_hash: str
    signal_type: str               # funding/hiring/buyer_post/news/semantic
    company_name: Optional[str]
    company_url: Optional[str]
    company_domain: Optional[str]
    raw_text: str
    source_url: Optional[str]
    source_platform: str
    funding_amount: Optional[float] = None
    funding_round: Optional[str] = None
    job_title: Optional[str] = None
    signal_date: Optional[datetime] = None
