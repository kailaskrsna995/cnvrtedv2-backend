/**
 * LEAD LIST
 * =========
 * Renders the full lead list for a profile.
 * Splits into LIVE SIGNALS (🔴) and POTENTIAL MATCHES (🟡).
 * Shows empty state if no leads.
 *
 * TODO (intern):
 *   1. Accept LeadList type as prop
 *   2. Render live_signals section with 🔴 header
 *   3. Render potential_matches section with 🟡 header
 *   4. Empty state: "No leads yet — hit Run Now to scan"
 *   5. Loading state: skeleton cards while fetching
 */

import { LeadList as LeadListType } from "@/lib/types"
import LeadCard from "./LeadCard"

interface Props {
  data: LeadListType | null
  loading?: boolean
  onDismiss?: (id: string) => void
}

export default function LeadList({ data, loading, onDismiss }: Props) {
  if (loading) {
    return (
      <div className="space-y-3">
        {[1,2,3].map(i => (
          <div key={i} className="border border-zinc-800 rounded-xl p-5 animate-pulse h-32 bg-zinc-900" />
        ))}
      </div>
    )
  }

  if (!data || data.total === 0) {
    return (
      <div className="text-center py-20 text-zinc-500">
        <p className="text-lg mb-2">No leads yet</p>
        <p className="text-sm">Hit Run Now to scan for signals</p>
      </div>
    )
  }

  return (
    <div className="space-y-8">

      {/* Live Signals */}
      {data.live_signals.length > 0 && (
        <section>
          <h2 className="text-sm font-bold text-red-500 uppercase tracking-widest mb-3">
            🔴 Live Signals ({data.live_signals.length})
          </h2>
          <div className="space-y-3">
            {data.live_signals.map(lead => (
              <LeadCard key={lead.id} lead={lead} onDismiss={onDismiss} />
            ))}
          </div>
        </section>
      )}

      {/* Potential Matches */}
      {data.potential_matches.length > 0 && (
        <section>
          <h2 className="text-sm font-bold text-yellow-500 uppercase tracking-widest mb-3">
            🟡 Potential Matches ({data.potential_matches.length})
          </h2>
          <div className="space-y-3">
            {data.potential_matches.map(lead => (
              <LeadCard key={lead.id} lead={lead} onDismiss={onDismiss} />
            ))}
          </div>
        </section>
      )}

    </div>
  )
}
