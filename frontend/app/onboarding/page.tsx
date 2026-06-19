"use client"

import { useState } from "react"
import { useRouter } from "next/navigation"
import ICPCard from "@/components/icp/ICPCard"
import { profilesApi } from "@/lib/api"
import { ICPOption } from "@/lib/types"

interface FormData {
  website_url: string
  linkedin_url: string
  service_description: string
  target_description: string
}

export default function OnboardingPage() {
  const [step, setStep] = useState<"form" | "loading" | "icp_options">("form")
  const [form, setForm] = useState<FormData>({
    website_url: "",
    linkedin_url: "",
    service_description: "",
    target_description: "",
  })
  const [icpOptions, setIcpOptions] = useState<ICPOption[]>([])
  const [selectedICP, setSelectedICP] = useState<ICPOption | null>(null)
  const [error, setError] = useState("")
  const [debug, setDebug] = useState<any>(null)
  const [saving, setSaving] = useState(false)
  const [userContext, setUserContext] = useState("")
  const router = useRouter()

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setError("")
    setStep("loading")

    try {
      const res = await profilesApi.generateICP({
        name: "My Profile",
        website_url: form.website_url,
        linkedin_url: form.linkedin_url || undefined,
        service_description: form.service_description,
        target_description: form.target_description,
      }) as any

      setIcpOptions(res.icp_options || [])
      setDebug(res._debug || null)
      setUserContext(res.user_context || "")
      setStep("icp_options")
    } catch (err: any) {
      setError(err.message || "Something went wrong")
      setStep("form")
    }
  }

  async function handleSelect(option: ICPOption) {
    setSaving(true)
    try {
      const res = await profilesApi.saveICP({
        name: "My Profile",
        website_url: form.website_url,
        linkedin_url: form.linkedin_url || undefined,
        service_description: form.service_description,
        target_description: form.target_description,
        chosen_icp_text: option.icp_text,
        user_context: userContext,
      }) as any
      router.push(`/dashboard?profile_id=${res.profile_id}`)
    } catch (err: any) {
      setError("Failed to save ICP. Please try again.")
      setSaving(false)
    }
  }

  if (selectedICP) {
    return (
      <main className="min-h-screen bg-black text-white p-8">
        <div className="max-w-2xl mx-auto">
          <div className="border border-green-700 rounded-xl p-6 bg-green-950/30">
            <p className="text-green-400 text-sm font-medium mb-1">ICP selected</p>
            <p className="text-white font-semibold">{selectedICP.label}</p>
            <p className="text-zinc-400 text-sm mt-1">{selectedICP.summary}</p>
          </div>
          <div className="flex items-center gap-3 text-zinc-400 text-sm mt-6">
            <div className="w-4 h-4 border border-white border-t-transparent rounded-full animate-spin" />
            Saving and redirecting to dashboard...
          </div>
        </div>
      </main>
    )
  }

  return (
    <main className="min-h-screen bg-black text-white p-8">
      <div className="max-w-2xl mx-auto">
        <h1 className="text-2xl font-bold mb-2">Set up your profile</h1>
        <p className="text-zinc-400 text-sm mb-8">
          We'll analyse your site and build your ideal customer profile.
        </p>

        {step === "form" && (
          <form onSubmit={handleSubmit} className="space-y-5">
            <div>
              <label className="block text-sm text-zinc-400 mb-1">Website URL</label>
              <input
                type="url"
                required
                placeholder="https://youragency.com"
                value={form.website_url}
                onChange={e => setForm({ ...form, website_url: e.target.value })}
                className="w-full bg-zinc-900 border border-zinc-700 rounded-lg px-4 py-3 text-white text-sm placeholder-zinc-600 focus:outline-none focus:border-zinc-400"
              />
            </div>

            <div>
              <label className="block text-sm text-zinc-400 mb-1">
                LinkedIn URL <span className="text-zinc-600">(optional)</span>
              </label>
              <input
                type="url"
                placeholder="https://linkedin.com/company/youragency"
                value={form.linkedin_url}
                onChange={e => setForm({ ...form, linkedin_url: e.target.value })}
                className="w-full bg-zinc-900 border border-zinc-700 rounded-lg px-4 py-3 text-white text-sm placeholder-zinc-600 focus:outline-none focus:border-zinc-400"
              />
            </div>

            <div>
              <label className="block text-sm text-zinc-400 mb-1">What do you sell?</label>
              <textarea
                required
                rows={2}
                placeholder="e.g. Performance marketing for B2B SaaS companies"
                value={form.service_description}
                onChange={e => setForm({ ...form, service_description: e.target.value })}
                className="w-full bg-zinc-900 border border-zinc-700 rounded-lg px-4 py-3 text-white text-sm placeholder-zinc-600 focus:outline-none focus:border-zinc-400 resize-none"
              />
            </div>

            <div>
              <label className="block text-sm text-zinc-400 mb-1">Who do you target?</label>
              <textarea
                required
                rows={2}
                placeholder="e.g. Series A–C SaaS, 20–200 employees, needs to scale paid acquisition"
                value={form.target_description}
                onChange={e => setForm({ ...form, target_description: e.target.value })}
                className="w-full bg-zinc-900 border border-zinc-700 rounded-lg px-4 py-3 text-white text-sm placeholder-zinc-600 focus:outline-none focus:border-zinc-400 resize-none"
              />
            </div>

            {error && <p className="text-red-400 text-sm">{error}</p>}

            <button
              type="submit"
              className="w-full bg-white text-black py-3 rounded-lg font-medium hover:bg-zinc-200 transition-colors"
            >
              Generate my ICP
            </button>
          </form>
        )}

        {step === "loading" && (
          <div className="flex flex-col items-center justify-center py-20 space-y-4">
            <div className="w-8 h-8 border-2 border-white border-t-transparent rounded-full animate-spin" />
            <p className="text-zinc-400 text-sm">Analysing your site and building your ICP…</p>
            <p className="text-zinc-600 text-xs">Usually takes 15–30 seconds</p>
          </div>
        )}

        {step === "icp_options" && (
          <div>
            <h2 className="text-lg font-semibold mb-1">Choose your ICP</h2>
            <p className="text-zinc-400 text-sm mb-6">
              Pick the profile that fits best. You can refine it later.
            </p>
            {saving && (
              <div className="flex items-center gap-3 text-zinc-400 text-sm mb-6">
                <div className="w-4 h-4 border border-white border-t-transparent rounded-full animate-spin" />
                Saving your ICP and generating vector...
              </div>
            )}
            <div className="space-y-4">
              {icpOptions.map((opt, i) => (
                <ICPCard key={i} option={opt} onSelect={saving ? () => {} : handleSelect} />
              ))}
            </div>
            <button
              className="mt-6 text-zinc-500 text-sm underline hover:text-zinc-300"
              onClick={() => setStep("form")}
            >
              Start over
            </button>

            {debug && (
              <div className="mt-8 border border-zinc-800 rounded-lg p-4 text-xs">
                <p className="text-zinc-500 font-mono mb-3">— debug: scrape results —</p>
                <div className="flex gap-4 mb-3 text-zinc-400">
                  <span>in: <span className="text-white">{debug.tokens_in} tokens</span></span>
                  <span>out: <span className="text-white">{debug.tokens_out} tokens</span></span>
                  <span>cost: <span className="text-green-400">${debug.cost_usd}</span></span>
                </div>
                <div className="space-y-3">
                  <div>
                    <p className="text-zinc-400 mb-1">Website <span className="text-zinc-600">({debug.website_chars} chars)</span></p>
                    <pre className="text-zinc-500 whitespace-pre-wrap bg-zinc-900 p-3 rounded">{debug.website_preview}</pre>
                  </div>
                  <div>
                    <p className="text-zinc-400 mb-1">LinkedIn <span className="text-zinc-600">({debug.linkedin_chars} chars)</span></p>
                    <pre className="text-zinc-500 whitespace-pre-wrap bg-zinc-900 p-3 rounded">{debug.linkedin_preview}</pre>
                  </div>
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </main>
  )
}
