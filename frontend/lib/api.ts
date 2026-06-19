// ============================================================
// API CLIENT
// All backend calls go through here. One place to change the base URL.
// ============================================================

const BASE_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8001"

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  })
  if (!res.ok) {
    const error = await res.json().catch(() => ({ detail: "Unknown error" }))
    throw new Error(error.detail || `HTTP ${res.status}`)
  }
  return res.json()
}

// ── Profiles ─────────────────────────────────────────────────

export const profilesApi = {
  saveICP: (data: {
    name: string
    website_url: string
    linkedin_url?: string
    service_description: string
    target_description: string
    chosen_icp_text: string
    user_context: string
    email?: string
  }) => request("/profiles/save-icp", {
    method: "POST",
    body: JSON.stringify(data),
  }),

  generateICP: (data: {
    name: string
    website_url: string
    linkedin_url?: string
    service_description: string
    target_description: string
  }) => request("/profiles/generate-icp", {
    method: "POST",
    body: JSON.stringify({ user_id: "local", ...data }),
  }),

  create: (data: {
    user_id: string
    name: string
    website_url: string
    linkedin_url?: string
    service_description: string
    target_description: string
  }) => request("/profiles/", { method: "POST", body: JSON.stringify(data) }),

  list: (user_id: string) =>
    request(`/profiles/${user_id}/all`),

  approveICP: (profile_id: string, chosen_icp_text: string, user_context_text: string) =>
    request(`/profiles/${profile_id}/approve`, {
      method: "POST",
      body: JSON.stringify({ profile_id, chosen_icp_text, user_context_text }),
    }),

  chat: (profile_id: string, message: string, history: any[]) =>
    request(`/profiles/${profile_id}/chat`, {
      method: "POST",
      body: JSON.stringify({ profile_id, message, history }),
    }),
}

// ── Leads ────────────────────────────────────────────────────

export const leadsApi = {
  triggerRun: (profile_id: string) =>
    request(`/leads/v2/run/${profile_id}`, { method: "POST" }),

  getResults: (profile_id: string) =>
    request(`/leads/v2/results/${profile_id}`),

  get: (profile_id: string) =>
    request(`/leads/v2/${profile_id}`),

  refresh: (profile_id: string) =>
    request(`/leads/v2/${profile_id}/refresh`, { method: "POST" }),

  updateStatus: (lead_id: string, status: string) =>
    request(`/leads/v2/${lead_id}/status`, {
      method: "PUT",
      body: JSON.stringify({ status }),
    }),

  coldList: (profile_id: string) =>
    request(`/leads/v2/coldlist/${profile_id}`),

  enrichColdList: (profile_id: string) =>
    request(`/leads/v2/coldlist/${profile_id}/enrich`, { method: "POST" }),

  coldListEnrichStatus: (profile_id: string) =>
    request(`/leads/v2/coldlist/${profile_id}/enrich-status`),

  competitors: (profile_id: string) =>
    request(`/leads/v2/competitors/${profile_id}`),

  validateColdList: (profile_id: string) =>
    request(`/leads/v2/coldlist/${profile_id}/validate`, { method: "POST" }),

  validateStatus: (profile_id: string) =>
    request(`/leads/v2/coldlist/${profile_id}/validate-status`),

  coldListFeedback: (profile_id: string, company_name: string, feedback: string | null) =>
    request(`/leads/v2/coldlist/${profile_id}/feedback`, {
      method: "POST",
      body: JSON.stringify({ company_name, feedback }),
    }),

  listClients: (profile_id: string) =>
    request(`/leads/v2/clients/${profile_id}`),

  addClient: (profile_id: string, company_name: string, company_domain?: string) =>
    request(`/leads/v2/clients/${profile_id}`, {
      method: "POST",
      body: JSON.stringify({ company_name, company_domain }),
    }),

  removeClient: (client_id: string) =>
    request(`/leads/v2/clients/${client_id}`, { method: "DELETE" }),
}

// ── Agents ───────────────────────────────────────────────────

export const agentsApi = {
  trigger: (profile_id: string, agent_names?: string[]) =>
    request("/agents/trigger", {
      method: "POST",
      body: JSON.stringify({ profile_id, agent_names }),
    }),

  status: () => request("/agents/status"),

  queueSize: () => request("/agents/queue"),
}
