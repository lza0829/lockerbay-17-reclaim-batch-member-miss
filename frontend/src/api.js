async function req(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts,
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export const api = {
  health: () => req('/api/health'),
  overview: () => req('/api/overview'),
  bay: () => req('/api/bay'),
  occupancies: () => req('/api/occupancies'),
  events: () => req('/api/events'),
  quarantine: () => req('/api/quarantine'),
  proofs: () => req('/api/occupancy-proofs'),
  reclaims: () => req('/api/reclaims'),
  runReclaim: (now) =>
    req('/api/reclaim/run', { method: 'POST', body: JSON.stringify(now ? { now } : {}) }),
  settings: () => req('/api/settings'),
  setOverdue: (overdue_hours) =>
    req('/api/settings/overdue-hours', { method: 'PUT', body: JSON.stringify({ overdue_hours }) }),
  ingest: () => req('/api/ingest/run', { method: 'POST', body: '{}' }),
  reclaimRuns: () => req('/api/reclaim/runs'),
  reclaimMembers: (runId) =>
    req(runId ? `/api/reclaim/run-members?run_id=${encodeURIComponent(runId)}` : '/api/reclaim/run-members'),
  reclaimAttest: (runId) => req(`/api/reclaim/attest?run_id=${encodeURIComponent(runId)}`),
}
