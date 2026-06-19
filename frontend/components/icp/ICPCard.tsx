/**
 * ICP CARD
 * ========
 * Shown during onboarding — displays one of the 3 generated ICP options.
 * User clicks "Use this" to approve it.
 *
 * TODO (intern):
 *   1. Show label (Broad / Niche / Signal-Based)
 *   2. Show summary (one-line)
 *   3. Expandable: click to see full icp_text
 *   4. "Use this" button calls onSelect(option)
 */

import { ICPOption } from "@/lib/types"

interface Props {
  option: ICPOption
  onSelect: (option: ICPOption) => void
}

export default function ICPCard({ option, onSelect }: Props) {
  return (
    <div className="border border-zinc-700 rounded-xl p-5 hover:border-zinc-500 transition-colors">
      <div className="flex items-start justify-between mb-2">
        <span className="text-xs font-bold text-zinc-400 uppercase tracking-widest">
          {option.label}
        </span>
      </div>
      <p className="text-white text-sm mb-4">{option.summary}</p>
      <details className="mb-4">
        <summary className="text-zinc-500 text-xs cursor-pointer hover:text-zinc-300">
          See full ICP
        </summary>
        <pre className="text-zinc-400 text-xs mt-2 whitespace-pre-wrap leading-relaxed">
          {option.icp_text}
        </pre>
      </details>
      <button
        onClick={() => onSelect(option)}
        className="w-full bg-white text-black py-2 rounded-lg text-sm font-medium hover:bg-zinc-200 transition-colors"
      >
        Use this
      </button>
    </div>
  )
}
