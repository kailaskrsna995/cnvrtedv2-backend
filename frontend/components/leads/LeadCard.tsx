/**
 * LEAD CARD
 * =========
 * One card per lead in the dashboard.
 *
 * Shows:
 *   Company name | Signal badge (🔴 FUNDING / 🟡 HIRING etc) | Score
 *   Why flagged
 *   Decision maker name + title
 *   Email (if available)
 *   Outreach line + [Copy] button
 *
 * TODO (intern):
 *   1. Render all fields from the Lead type
 *   2. Copy button copies outreach_line to clipboard
 *   3. Dismiss button calls leadsApi.updateStatus(id, "dismissed")
 *   4. Signal badge colour: funding=red, hiring=orange, buyer_post=blue, news=purple
 */

import { Lead } from "@/lib/types"

const SIGNAL_BADGE: Record<string, { label: string; color: string }> = {
  funding:     { label: "FUNDING",      color: "bg-red-500" },
  hiring:      { label: "HIRING",       color: "bg-orange-500" },
  buyer_post:  { label: "BUYER POST",   color: "bg-blue-500" },
  news:        { label: "NEWS",         color: "bg-purple-500" },
  semantic:    { label: "INTENT",       color: "bg-green-500" },
  icp_match:   { label: "ICP MATCH",   color: "bg-zinc-500" },
}

interface Props {
  lead: Lead
  onDismiss?: (id: string) => void
}

export default function LeadCard({ lead, onDismiss }: Props) {
  const badge = SIGNAL_BADGE[lead.signal_type] || { label: lead.signal_type, color: "bg-zinc-600" }
  const score = Math.round((lead.intent_score || 0) * 100)

  const copyOutreach = () => {
    if (lead.outreach_line) {
      navigator.clipboard.writeText(lead.outreach_line)
      // TODO: show a toast "Copied!"
    }
  }

  return (
    <div className="border border-zinc-800 rounded-xl p-5 space-y-3 bg-zinc-950">

      {/* Header */}
      <div className="flex items-start justify-between">
        <div>
          <span className="font-semibold text-white">{lead.company_name || "Unknown Company"}</span>
          {lead.company_url && (
            <a href={lead.company_url} target="_blank" className="ml-2 text-zinc-500 text-xs hover:text-white">
              ↗
            </a>
          )}
        </div>
        <div className="flex items-center gap-2">
          <span className={`text-xs font-bold px-2 py-0.5 rounded ${badge.color} text-white`}>
            {badge.label}
          </span>
          <span className="text-zinc-400 text-sm">{score}%</span>
        </div>
      </div>

      {/* Why flagged */}
      {lead.why_flagged && (
        <p className="text-zinc-400 text-sm">{lead.why_flagged}</p>
      )}

      {/* Decision maker */}
      {lead.decision_maker && (
        <div className="text-sm">
          <span className="text-white">{lead.decision_maker}</span>
          {lead.title && <span className="text-zinc-500">, {lead.title}</span>}
          {lead.email && (
            <span className="ml-2 text-zinc-400">{lead.email}</span>
          )}
        </div>
      )}

      {/* Outreach line */}
      {lead.outreach_line && (
        <div className="flex items-start justify-between gap-3 bg-zinc-900 rounded-lg p-3">
          <p className="text-zinc-300 text-sm italic flex-1">&ldquo;{lead.outreach_line}&rdquo;</p>
          <button
            onClick={copyOutreach}
            className="text-zinc-500 hover:text-white text-xs shrink-0 border border-zinc-700 rounded px-2 py-1"
          >
            Copy
          </button>
        </div>
      )}

      {/* Actions */}
      <div className="flex gap-2 pt-1">
        {lead.source_url && (
          <a
            href={lead.source_url}
            target="_blank"
            className="text-zinc-500 text-xs hover:text-white"
          >
            View source ↗
          </a>
        )}
        <button
          onClick={() => onDismiss?.(lead.id)}
          className="text-zinc-600 text-xs hover:text-zinc-400 ml-auto"
        >
          Dismiss
        </button>
      </div>

    </div>
  )
}
