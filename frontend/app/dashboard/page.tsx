"use client"

import { useState, useRef, useEffect, Suspense } from "react"
import { useSearchParams } from "next/navigation"
import { leadsApi } from "@/lib/api"

interface PipelineStage {
  name: string
  status: string
  detail: Record<string, any>
  error?: string | null
}

interface Signal {
  company_name: string
  company_domain: string
  funding_round: string
  funding_amount: string
  summary: string
  source_url: string
  intent_score: number
  why: string
  signal_type?: string
  source_platform?: string
  match_score?: number | null
  proof?: string
  outreach?: string
  evidence_type?: string
  signal_count?: number
  distinct_signal_types?: string[]
  sources?: { url: string; summary: string; signal_type: string }[]
  contact_name?: string
  contact_title?: string
  contact_linkedin?: string
}

function Dashboard() {
  const searchParams = useSearchParams()
  const profile_id = searchParams.get("profile_id") || ""

  const [running, setRunning] = useState(false)
  const [leads, setLeads] = useState<Signal[] | null>(null)
  const [error, setError] = useState("")
  const [pipeline, setPipeline] = useState<PipelineStage[]>([])
  const [showTrace, setShowTrace] = useState(true)
  const [tab, setTab] = useState<"leads" | "intent" | "targets" | "competitors">("leads")
  const [cold, setCold] = useState<{ total: number; with_contact: number; companies: any[] } | null>(null)
  const [competitors, setCompetitors] = useState<{ name: string; url: string }[] | null>(null)
  const [enriching, setEnriching] = useState(false)
  const enrichPoll = useRef<any>(null)
  const poll = useRef<any>(null)

  async function loadColdList() {
    if (!profile_id) return
    try {
      const res = await leadsApi.coldList(profile_id) as any
      setCold(res)
    } catch { /* ignore */ }
  }

  async function enrichContacts() {
    if (!profile_id || enriching) return
    setEnriching(true)
    try {
      await leadsApi.enrichColdList(profile_id)
    } catch { setEnriching(false); return }
    enrichPoll.current = setInterval(async () => {
      try {
        const s = await leadsApi.coldListEnrichStatus(profile_id) as any
        await loadColdList()
        if (s.status === "done" || s.status === "idle") {
          clearInterval(enrichPoll.current)
          setEnriching(false)
        }
      } catch { clearInterval(enrichPoll.current); setEnriching(false) }
    }, 3000)
  }

  const [validating, setValidating] = useState(false)
  const validatePoll = useRef<any>(null)

  // Scan timer — live elapsed while running, last duration persisted
  const [elapsed, setElapsed] = useState(0)
  const [lastDuration, setLastDuration] = useState<number | null>(null)
  const scanStart = useRef<number | null>(null)

  useEffect(() => {
    const saved = typeof window !== "undefined" ? window.localStorage.getItem("cnvrted_last_scan_secs") : null
    if (saved) setLastDuration(parseInt(saved))
  }, [])

  useEffect(() => {
    let t: any
    if (running) {
      if (!scanStart.current) scanStart.current = Date.now()
      t = setInterval(() => setElapsed(Math.floor((Date.now() - (scanStart.current || Date.now())) / 1000)), 1000)
    } else if (scanStart.current) {
      const dur = Math.floor((Date.now() - scanStart.current) / 1000)
      setLastDuration(dur)
      try { window.localStorage.setItem("cnvrted_last_scan_secs", String(dur)) } catch {}
      scanStart.current = null
      setElapsed(0)
    }
    return () => clearInterval(t)
  }, [running])

  const fmtSecs = (s: number) => s >= 60 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${s}s`

  async function validateList() {
    if (!profile_id || validating) return
    setValidating(true)
    try { await leadsApi.validateColdList(profile_id) } catch { setValidating(false); return }
    validatePoll.current = setInterval(async () => {
      try {
        const s = await leadsApi.validateStatus(profile_id) as any
        if (s.status === "done" || s.status === "idle" || s.status === "error") {
          clearInterval(validatePoll.current)
          setValidating(false)
          await loadColdList()
        }
      } catch { clearInterval(validatePoll.current); setValidating(false) }
    }, 3000)
  }

  function openTargets() {
    setTab("targets")
    loadColdList()  // always refresh — watchlist may have just been built
  }

  function openCompetitors() {
    setTab("competitors")
    if (profile_id) leadsApi.competitors(profile_id).then((r: any) => setCompetitors(r.competitors || [])).catch(() => {})
  }

  async function sendFeedback(company_name: string, feedback: string | null) {
    if (!profile_id) return
    // optimistic: drop disliked rows immediately
    if (cold) {
      setCold({ ...cold,
        companies: feedback === "disliked"
          ? cold.companies.filter(c => c.company_name !== company_name)
          : cold.companies.map(c => c.company_name === company_name ? { ...c, feedback } : c)
      })
    }
    try { await leadsApi.coldListFeedback(profile_id, company_name, feedback) } catch {}
  }

  // Intent Leads = STATED intent only (someone literally asking/seeking).
  // Company Leads = trigger events (funding/news/launches/hires) — inferred intent.
  // Classified by evidence_type, not source, so trigger-type posts move to Company.
  const displayedLeads = leads
    ? leads.filter(l => {
        const isStatedIntent = l.evidence_type === "stated_intent"
        return tab === "intent" ? isStatedIntent : !isStatedIntent
      })
    : null

  function startPolling() {
    clearInterval(poll.current)
    poll.current = setInterval(async () => {
      try {
        const res = await leadsApi.getResults(profile_id) as any
        if (res.pipeline?.stages) setPipeline(res.pipeline.stages)
        if (res.status === "done") {
          clearInterval(poll.current)
          setLeads(res.leads || [])
          setRunning(false)
        } else if (res.status === "error") {
          clearInterval(poll.current)
          setError(res.error || "Something went wrong")
          setRunning(false)
        }
      } catch (e) {
        clearInterval(poll.current)
        setError("Lost connection")
        setRunning(false)
      }
    }, 4000)
  }

  // On open: load whatever the backend already has (run triggered elsewhere / last result)
  useEffect(() => {
    if (!profile_id) return
    leadsApi.getResults(profile_id).then((res: any) => {
      if (res.pipeline?.stages) setPipeline(res.pipeline.stages)
      if (res.status === "running") {
        setRunning(true)
        startPolling()
      } else if (res.status === "done") {
        setLeads(res.leads || [])
      }
    }).catch(() => {})
    return () => clearInterval(poll.current)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [profile_id])

  async function runNow() {
    if (!profile_id || running) return
    setRunning(true)
    setLeads(null)
    setError("")
    setPipeline([])

    try {
      await leadsApi.triggerRun(profile_id)
    } catch (e: any) {
      setError("Failed to start: " + e.message)
      setRunning(false)
      return
    }
    startPolling()
  }

  function download(csv: string, name: string) {
    const a = document.createElement("a")
    a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" }))
    const date = new Date().toISOString().slice(0, 10)
    a.download = `cnvrted-${name}-${date}.csv`  // no profile id in filename
    a.click()
  }

  function csvCell(v: any) { return `"${String(v ?? "").replace(/"/g, "'")}"` }

  function exportLeads(rows: Signal[], name: string) {
    if (!rows || !rows.length) return
    const header = "Company,Type,Evidence,Contact,ContactTitle,ContactLinkedIn,Score,Why,Proof,Outreach,Source"
    const lines = rows.map(r => [
      csvCell(r.company_name), csvCell(r.signal_type), csvCell(r.evidence_type),
      csvCell(r.contact_name), csvCell(r.contact_title), csvCell(r.contact_linkedin),
      r.intent_score?.toFixed(2) ?? "", csvCell(r.why), csvCell(r.proof), csvCell(r.outreach), csvCell(r.source_url),
    ].join(","))
    download([header, ...lines].join("\n"), name)
  }

  function exportTargets() {
    if (!cold?.companies?.length) return
    const header = "Company,WhyInICP,Proof,ProofURL,Contact,ContactTitle,ContactLinkedIn"
    const lines = cold.companies.map((c: any) => [
      csvCell(c.company_name), csvCell(c.reason), csvCell(c.proof_summary), csvCell(c.proof_url),
      csvCell(c.contact_name), csvCell(c.contact_title), csvCell(c.contact_linkedin),
    ].join(","))
    download([header, ...lines].join("\n"), "target-list")
  }

  return (
    <main className="min-h-screen bg-black text-white">
      <div className="border-b border-zinc-800 px-6 py-4 flex items-center justify-between">
        <span className="font-bold text-lg">cnvrted</span>
        <div className="flex items-center gap-3">
          {running ? (
            <span className="font-mono text-sm text-zinc-400">⏱ {fmtSecs(elapsed)}</span>
          ) : lastDuration != null && (
            <span className="font-mono text-xs text-zinc-600" title="Last scan duration">last scan: {fmtSecs(lastDuration)}</span>
          )}
          {tab === "targets" && cold?.companies?.length ? (
            <button onClick={exportTargets} className="border border-zinc-700 text-zinc-300 px-3 py-1.5 rounded text-sm">
              Export Target List
            </button>
          ) : (tab === "leads" || tab === "intent") && displayedLeads && displayedLeads.length > 0 ? (
            <button onClick={() => exportLeads(displayedLeads, tab === "intent" ? "intent-leads" : "company-leads")}
              className="border border-zinc-700 text-zinc-300 px-3 py-1.5 rounded text-sm">
              Export {tab === "intent" ? "Intent" : "Company"} Leads
            </button>
          ) : null}
          <button
            onClick={runNow}
            disabled={running || !profile_id}
            className="bg-white text-black px-4 py-2 rounded text-sm font-medium disabled:opacity-50"
          >
            {running ? "Scanning..." : "Run Now"}
          </button>
        </div>
      </div>

      {/* Tabs */}
      <div className="border-b border-zinc-800 px-6 flex gap-6">
        <button
          onClick={() => setTab("leads")}
          className={`py-3 text-sm border-b-2 ${tab === "leads" ? "border-white text-white" : "border-transparent text-zinc-500"}`}
        >
          Company Leads
        </button>
        <button
          onClick={() => setTab("intent")}
          className={`py-3 text-sm border-b-2 ${tab === "intent" ? "border-white text-white" : "border-transparent text-zinc-500"}`}
        >
          Intent Leads
        </button>
        <button
          onClick={openTargets}
          className={`py-3 text-sm border-b-2 ${tab === "targets" ? "border-white text-white" : "border-transparent text-zinc-500"}`}
        >
          Target List {cold && <span className="text-zinc-600">({cold.total})</span>}
        </button>
        <button
          onClick={openCompetitors}
          className={`py-3 text-sm border-b-2 ${tab === "competitors" ? "border-white text-white" : "border-transparent text-zinc-500"}`}
        >
          Competitors {competitors && <span className="text-zinc-600">({competitors.length})</span>}
        </button>
      </div>

      {/* COMPETITORS */}
      {tab === "competitors" && (
        <div className="p-6">
          <p className="text-zinc-400 text-sm mb-4">Companies in your space — excluded from leads, shown here as market intel.</p>
          {!competitors ? (
            <p className="text-zinc-500 text-sm">Loading…</p>
          ) : competitors.length === 0 ? (
            <p className="text-zinc-500 text-sm">No competitors identified yet.</p>
          ) : (
            <div className="overflow-x-auto rounded-lg border border-zinc-800">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-zinc-800 bg-zinc-900/50">
                    {["Competitor", "Site"].map(h => (
                      <th key={h} className="text-left px-4 py-3 text-zinc-400 font-medium">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {competitors.map((c, i) => (
                    <tr key={i} className="border-b border-zinc-800/50 hover:bg-zinc-900/30">
                      <td className="px-4 py-3 font-medium">{c.name}</td>
                      <td className="px-4 py-3">
                        {c.url ? <a href={c.url} target="_blank" rel="noopener noreferrer" className="text-zinc-400 text-xs underline hover:text-white">{c.url.replace(/^https?:\/\//, "").slice(0, 40)} ›</a> : <span className="text-zinc-600 text-xs">—</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {/* TARGET LIST (cold list) */}
      {tab === "targets" && (
        <div className="p-6">
          <div className="flex items-center justify-between mb-4">
            <p className="text-zinc-400 text-sm">
              In-ICP companies to reach out to{cold ? ` · ${cold.with_proof ?? 0}/${cold.total} proven · ${cold.with_contact}/${cold.total} with contacts` : ""}
            </p>
            <div className="flex gap-2">
              <button
                onClick={validateList}
                disabled={validating || !profile_id}
                className="border border-zinc-700 text-zinc-300 px-3 py-1.5 rounded text-sm disabled:opacity-50"
                title="Check each company for recent activity — drops defunct/dormant ones, attaches proof"
              >
                {validating ? "Validating..." : "Validate + find proof"}
              </button>
              <button
                onClick={enrichContacts}
                disabled={enriching || !profile_id}
                className="bg-white text-black px-3 py-1.5 rounded text-sm font-medium disabled:opacity-50"
              >
                {enriching ? "Finding contacts..." : "Find contacts"}
              </button>
            </div>
          </div>
          {!cold ? (
            <p className="text-zinc-500 text-sm">Loading…</p>
          ) : (
            <div className="overflow-x-auto rounded-lg border border-zinc-800">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-zinc-800 bg-zinc-900/50">
                    {["Company", "Why in-ICP", "Proof", "Contact", "LinkedIn", ""].map(h => (
                      <th key={h} className="text-left px-4 py-3 text-zinc-400 font-medium">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {cold.companies.map((c, i) => (
                    <tr key={i} className="border-b border-zinc-800/50 hover:bg-zinc-900/30">
                      <td className="px-4 py-3 font-medium">{c.company_name}</td>
                      <td className="px-4 py-3 text-zinc-500 text-xs max-w-md">{c.reason}</td>
                      <td className="px-4 py-3 text-xs max-w-xs">
                        {c.proof_url ? (
                          <a href={c.proof_url} target="_blank" rel="noopener noreferrer" className="text-green-400 hover:text-green-300" title={c.proof_summary}>
                            ✓ {(c.proof_summary || "recent activity").slice(0, 50)} ›
                          </a>
                        ) : <span className="text-zinc-600">unverified</span>}
                      </td>
                      <td className="px-4 py-3 text-xs">
                        {c.contact_name ? (
                          <span><span className="text-zinc-200">{c.contact_name}</span><span className="block text-zinc-500">{c.contact_title}</span></span>
                        ) : <span className="text-zinc-600">—</span>}
                      </td>
                      <td className="px-4 py-3">
                        {c.contact_linkedin
                          ? <a href={c.contact_linkedin} target="_blank" rel="noopener noreferrer" className="text-zinc-400 text-xs underline hover:text-white">profile ›</a>
                          : <span className="text-zinc-600 text-xs">—</span>}
                      </td>
                      <td className="px-4 py-3 whitespace-nowrap">
                        <button onClick={() => sendFeedback(c.company_name, c.feedback === "liked" ? null : "liked")}
                          className={`text-sm mr-2 ${c.feedback === "liked" ? "opacity-100" : "opacity-40 hover:opacity-80"}`}
                          title="More like this">👍</button>
                        <button onClick={() => sendFeedback(c.company_name, "disliked")}
                          className="text-sm opacity-40 hover:opacity-80" title="Remove / fewer like this">👎</button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {(tab === "leads" || tab === "intent") && (
      <div className="p-6">
        {!profile_id && (
          <p className="text-zinc-500">No profile. <a href="/onboarding" className="underline text-white">Set up here</a></p>
        )}

        {profile_id && !running && leads === null && !error && (
          <p className="text-zinc-500 text-sm mt-8 text-center">Click <b className="text-white">Run Now</b> to find leads matching your ICP.</p>
        )}

        {running && (
          <div className="flex items-center gap-3 text-zinc-400 text-sm mt-8">
            <div className="w-4 h-4 border border-white border-t-transparent rounded-full animate-spin" />
            Scanning all signal sources and matching to your ICP...
          </div>
        )}

        {error && <p className="text-red-400 text-sm mt-4">{error}</p>}

        {/* Pipeline trace — what each module did, live */}
        {pipeline.length > 0 && (
          <div className="mt-6 border border-zinc-800 rounded-lg overflow-hidden">
            <button
              onClick={() => setShowTrace(!showTrace)}
              className="w-full flex items-center justify-between px-4 py-2.5 bg-zinc-900/50 text-left"
            >
              <span className="text-zinc-400 text-xs font-mono">PIPELINE TRACE — {pipeline.length} stages</span>
              <span className="text-zinc-600 text-xs">{showTrace ? "hide" : "show"}</span>
            </button>
            {showTrace && (
              <div className="divide-y divide-zinc-800/50">
                {pipeline.map((stage, i) => (
                  <div key={i} className="px-4 py-2.5 flex items-start gap-3">
                    {stage.status === "running" ? (
                      <div className="mt-0.5 w-3 h-3 border border-zinc-400 border-t-transparent rounded-full animate-spin" />
                    ) : (
                      <span className={`mt-0.5 text-xs ${
                        stage.status === "ok" ? "text-green-400" : "text-red-400"
                      }`}>
                        {stage.status === "ok" ? "✓" : "✗"}
                      </span>
                    )}
                    <div className="flex-1 min-w-0">
                      <span className="text-sm text-zinc-200">{stage.name}</span>
                      {stage.error ? (
                        <p className="text-red-400 text-xs mt-0.5">{stage.error}</p>
                      ) : (
                        <div className="flex flex-wrap gap-x-4 gap-y-0.5 mt-0.5">
                          {Object.entries(stage.detail || {}).map(([k, v]) => (
                            <span key={k} className="text-xs text-zinc-500 font-mono">
                              {k}: <span className="text-zinc-300">{typeof v === "object" ? JSON.stringify(v) : String(v)}</span>
                            </span>
                          ))}
                        </div>
                      )}
                    </div>
                  </div>
                ))}
                {running && (
                  <div className="px-4 py-2.5 flex items-center gap-3">
                    <div className="w-3 h-3 border border-zinc-500 border-t-transparent rounded-full animate-spin" />
                    <span className="text-xs text-zinc-500">next stage running...</span>
                  </div>
                )}
              </div>
            )}
          </div>
        )}

        {displayedLeads !== null && displayedLeads.length === 0 && !running && (
          <p className="text-zinc-500 text-sm mt-8 text-center">
            {tab === "intent" ? "No intent-based leads this run." : "No company leads matched your ICP this run."}
          </p>
        )}

        {displayedLeads && displayedLeads.length > 0 && (
          <div className="overflow-x-auto rounded-lg border border-zinc-800 mt-4">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-zinc-800 bg-zinc-900/50">
                  {["Company", "Type", "Contact", "Match", "Score", "Why", tab === "intent" ? "Buying intent (quoted)" : "Trigger (quoted)", "Outreach opener", "Source"].map(h => (
                    <th key={h} className="text-left px-4 py-3 text-zinc-400 font-medium">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {displayedLeads.map((l, i) => (
                  <tr key={i} className="border-b border-zinc-800/50 hover:bg-zinc-900/30">
                    <td className="px-4 py-3 font-medium">
                      {l.company_name || "—"}
                      {l.signal_count && l.signal_count > 1 && (
                        <span className="ml-2 text-xs text-zinc-500 font-normal">{l.signal_count} sources</span>
                      )}
                    </td>
                    <td className="px-4 py-3">
                      <span className={`text-xs px-2 py-0.5 rounded ${
                        l.signal_type === "buyer_intent" ? "bg-orange-900/50 text-orange-400" :
                        l.signal_type === "news" ? "bg-purple-900/50 text-purple-400" :
                        l.signal_type === "watchlist" ? "bg-green-900/50 text-green-400" :
                        "bg-blue-900/50 text-blue-400"
                      }`}>
                        {l.signal_type === "buyer_intent" ? `intent · ${l.source_platform || "web"}` :
                         l.signal_type === "news" ? `news · ${l.funding_round || "event"}` :
                         l.signal_type === "watchlist" ? "watchlist" :
                         "funding"}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-xs">
                      {l.contact_name ? (
                        <a href={l.contact_linkedin} target="_blank" rel="noopener noreferrer" className="hover:text-white">
                          <span className="text-zinc-200">{l.contact_name}</span>
                          <span className="block text-zinc-500">{l.contact_title}</span>
                        </a>
                      ) : <span className="text-zinc-600">—</span>}
                    </td>
                    <td className="px-4 py-3">
                      {l.match_score != null
                        ? <span className="font-mono text-xs text-zinc-400" title="Vector similarity to your ICP">{l.match_score.toFixed(2)}</span>
                        : <span className="text-zinc-600 text-xs">—</span>}
                    </td>
                    <td className="px-4 py-3">
                      <span className={`font-mono text-xs px-2 py-0.5 rounded ${
                        l.intent_score >= 0.7 ? "bg-green-900/50 text-green-400" :
                        l.intent_score >= 0.5 ? "bg-yellow-900/50 text-yellow-400" :
                        "bg-zinc-800 text-zinc-500"
                      }`}>{l.intent_score.toFixed(2)}</span>
                    </td>
                    <td className="px-4 py-3 text-zinc-400 text-xs max-w-xs">{l.why}</td>
                    <td className="px-4 py-3 text-xs max-w-xs">
                      {l.proof ? (
                        <span>
                          <span className={`text-[10px] uppercase mr-1 ${l.evidence_type === "stated_intent" ? "text-orange-400" : "text-zinc-500"}`}>
                            {l.evidence_type === "stated_intent" ? "intent" : "trigger"}
                          </span>
                          <span className="text-green-300 italic">“{l.proof}”</span>
                        </span>
                      ) : <span className="text-zinc-500">{l.summary}</span>}
                    </td>
                    <td className="px-4 py-3 text-xs max-w-xs">
                      {l.outreach ? (
                        <div className="group flex items-start gap-1.5">
                          <span className="text-zinc-300 italic">{l.outreach}</span>
                          <button
                            onClick={() => navigator.clipboard.writeText(l.outreach || "")}
                            title="Copy opener"
                            className="opacity-0 group-hover:opacity-100 transition shrink-0 text-zinc-500 hover:text-white"
                          >⧉</button>
                        </div>
                      ) : <span className="text-zinc-600">—</span>}
                    </td>
                    <td className="px-4 py-3">
                      {l.sources && l.sources.length > 0 ? (
                        <div className="flex flex-col gap-1">
                          {l.sources.slice(0, 5).map((s, si) => (
                            <a key={si} href={s.url} target="_blank" rel="noopener noreferrer"
                               className="text-zinc-500 text-xs underline hover:text-white whitespace-nowrap">
                              {s.signal_type || "link"} ›
                            </a>
                          ))}
                          {l.sources.length > 5 && (
                            <span className="text-zinc-600 text-xs">+{l.sources.length - 5} more</span>
                          )}
                        </div>
                      ) : l.source_url ? (
                        <a href={l.source_url} target="_blank" rel="noopener noreferrer" className="text-zinc-500 text-xs underline hover:text-white">link</a>
                      ) : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
      )}
    </main>
  )
}

export default function DashboardPage() {
  return <Suspense><Dashboard /></Suspense>
}
