// ============================================================
// CNVRTED V2 — TypeScript Types
// All shared types live here. Import from this file everywhere.
// ============================================================

export type SignalType = "funding" | "hiring" | "buyer_post" | "news" | "semantic" | "icp_match"

export type LeadStatus = "new" | "viewed" | "saved" | "dismissed"

export interface Profile {
  id: string
  name: string
  service_description: string
  icp_text: string
  is_active: boolean
  created_at: string
}

export interface ICPOption {
  label: string       // "Broad ICP" | "Niche ICP" | "Signal-Based ICP"
  summary: string     // one-line shown in UI card
  icp_text: string    // full ICP text
}

export interface Lead {
  id: string
  company_name: string
  company_url: string
  signal_type: SignalType
  why_flagged: string
  intent_score: number          // 0.0 to 1.0
  decision_maker: string
  title: string
  email: string
  phone: string
  linkedin_url: string
  outreach_line: string
  source_url: string
  signal_date: string
  status: LeadStatus
}

export interface LeadList {
  profile_id: string
  live_signals: Lead[]          // 🔴 active trigger events
  potential_matches: Lead[]     // 🟡 ICP-matched fallback
  total: number
}

export interface AgentStatus {
  agent_name: string
  status: "running" | "completed" | "failed"
  signals_found: number
  started_at: string
  completed_at: string | null
  error_message: string | null
}

export interface ChatMessage {
  role: "user" | "assistant"
  content: string
}
