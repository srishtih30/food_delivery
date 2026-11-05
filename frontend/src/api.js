import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react'

/* ------------------------------------------------------------------ */
/* Connection status shared by every request and the health poller.    */
/* ------------------------------------------------------------------ */
let connection = { online: null, checkedAt: 0, message: '' }
const listeners = new Set()
function setConnection(next) {
  if (next.online === connection.online && next.message === connection.message) return
  connection = { ...connection, ...next, checkedAt: Date.now() }
  listeners.forEach(fn => fn())
}
export function useConnection() {
  return useSyncExternalStore(fn => { listeners.add(fn); return () => listeners.delete(fn) }, () => connection)
}

export class ApiError extends Error {
  constructor(message, { status = 0, code = '', kind = 'http' } = {}) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.kind = kind
  }
}

let sequence = 0
const requestId = () => `${Date.now().toString(36)}-${(++sequence).toString(36)}-${Math.random().toString(36).slice(2, 7)}`

/** Ask the backend to stop a computation the browser no longer waits for. */
function cancelOnServer(id) {
  try {
    fetch('/api/cancel', { method: 'POST', keepalive: true, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id }) }).catch(() => {})
  } catch { /* best effort */ }
}

const OFFLINE = 'Could not reach the simulator API. Start the backend (python run.py in backend/) and try again.'

/**
 * fetch wrapper with a client-side timeout. Every request carries an
 * X-Request-Id; when it is aborted (by the caller or by the timeout) the
 * server is told to stop working on it, and it also notices the dropped
 * connection on its own.
 */
export async function api(path, options = {}) {
  const { signal, timeout = 30000, ...rest } = options
  const id = requestId()
  const controller = new AbortController()
  let reason = ''
  const onAbort = () => { reason = 'aborted'; controller.abort() }
  if (signal) {
    if (signal.aborted) onAbort()
    else signal.addEventListener('abort', onAbort, { once: true })
  }
  const timer = timeout ? setTimeout(() => { reason = 'timeout'; controller.abort() }, timeout) : null
  try {
    let response
    try {
      response = await fetch(`/api${path}`, {
        ...rest,
        signal: controller.signal,
        headers: { 'X-Request-Id': id, ...(rest.body ? { 'Content-Type': 'application/json' } : {}), ...rest.headers },
      })
    } catch (error) {
      if (controller.signal.aborted) throw error
      setConnection({ online: false, message: OFFLINE })
      throw new ApiError(OFFLINE, { kind: 'offline' })
    }
    const type = response.headers.get('content-type') || ''
    const body = type.includes('application/json') ? await response.json() : await response.text()
    if (response.status === 502 || response.status === 504) {
      // Vite's proxy answers 502/504 when the backend is down.
      setConnection({ online: false, message: OFFLINE })
      throw new ApiError(OFFLINE, { status: response.status, kind: 'offline' })
    }
    setConnection({ online: true, message: '' })
    if (!response.ok) {
      const detail = body && typeof body === 'object' ? body.error || body.message || body.detail : null
      throw new ApiError(typeof detail === 'string' ? detail : `Simulator request failed (HTTP ${response.status}).`,
        { status: response.status, code: body?.code || '', kind: response.status === 408 ? 'timeout' : 'http' })
    }
    if (!type.includes('application/json')) throw new ApiError('The simulator returned an unexpected non-JSON response.')
    return body
  } catch (error) {
    if (controller.signal.aborted) {
      cancelOnServer(id)
      if (reason === 'timeout') {
        throw new ApiError(`No answer after ${Math.round(timeout / 1000)} s, so the request was cancelled and the server was told to stop. Try a smaller scenario.`, { kind: 'timeout' })
      }
      const abort = new Error('Request cancelled')
      abort.name = 'AbortError'
      throw abort
    }
    throw error
  } finally {
    if (timer) clearTimeout(timer)
    signal?.removeEventListener('abort', onAbort)
  }
}

export function query(path, params) {
  const q = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (Array.isArray(value)) value.forEach(v => v != null && v !== '' && q.append(key, String(v)))
    else if (value !== undefined && value !== null && value !== '') q.set(key, String(value))
  })
  return `${path}?${q.toString()}`
}

/** GET a resource whenever `url` changes (null = nothing to load). */
export function useResource(url, { timeout = 20000 } = {}) {
  const [state, setState] = useState({ data: null, loading: Boolean(url), error: null })
  const [retryCount, setRetryCount] = useState(0)
  const retry = useCallback(() => setRetryCount(n => n + 1), [])
  useEffect(() => {
    if (!url) {
      setState({ data: null, loading: false, error: null })
      return
    }
    const controller = new AbortController()
    setState(previous => ({ ...previous, loading: true, error: null }))
    api(url, { signal: controller.signal, timeout })
      .then(data => setState({ data, loading: false, error: null }))
      .catch(error => {
        if (error.name !== 'AbortError') setState(previous => ({ ...previous, loading: false, error: error.message }))
      })
    return () => controller.abort()
  }, [url, retryCount, timeout])
  return { ...state, retry }
}

/**
 * Imperative request with busy/error state, elapsed time and cancel().
 * Starting a new request aborts the previous one (server included).
 */
export function useRequest({ timeout = 45000 } = {}) {
  const [state, setState] = useState({ busy: false, error: '', startedAt: 0 })
  const current = useRef(null)
  useEffect(() => () => current.current?.abort(), [])
  const run = useCallback(async (path, options = {}) => {
    current.current?.abort()
    const controller = new AbortController()
    current.current = controller
    setState({ busy: true, error: '', startedAt: Date.now() })
    try {
      const data = await api(path, { timeout, ...options, signal: controller.signal })
      if (current.current === controller) setState({ busy: false, error: '', startedAt: 0 })
      return data
    } catch (error) {
      if (error.name === 'AbortError') return undefined
      if (current.current === controller) setState({ busy: false, error: error.message, startedAt: 0 })
      return undefined
    }
  }, [timeout])
  const cancel = useCallback(() => {
    if (!current.current) return
    current.current.abort()
    current.current = null
    setState({ busy: false, error: 'Cancelled. The server has been told to stop this computation.', startedAt: 0 })
  }, [])
  const clearError = useCallback(() => setState(s => ({ ...s, error: '' })), [])
  return { ...state, run, cancel, clearError }
}

/** Polls /api/health so the shell can say when the backend goes away. */
export function useHealthPoll(interval = 8000) {
  useEffect(() => {
    let stopped = false
    const check = () => api('/health', { timeout: 4000 }).catch(() => {})
    check()
    const timer = setInterval(() => { if (!stopped && document.visibilityState === 'visible') check() }, interval)
    return () => { stopped = true; clearInterval(timer) }
  }, [interval])
}

/* ------------------------------------------------------------------ */
/* Formatting                                                          */
/* ------------------------------------------------------------------ */
const finite = value => value != null && Number.isFinite(Number(value))
export const number = value => !finite(value) ? '—' : new Intl.NumberFormat('en-IN', { maximumFractionDigits: 0 }).format(Number(value))
export const decimal = (value, digits = 1) => !finite(value) ? '—' : Number(value).toFixed(digits)
export const minutes = value => !finite(value) ? '—' : `${decimal(value)} min`
export const kilometers = (value, digits = 2) => !finite(value) ? '—' : `${decimal(value, digits)} km`
export const percent = (value, digits = 1) => !finite(value) ? '—' : `${(Number(value) * 100).toFixed(digits)}%`

export const nameFor = (id, nodes = [], catalog = {}) =>
  [...nodes, ...(catalog.restaurants || []), ...(catalog.societies || [])].find(item => item.id === id)?.name || id || '—'

export function routeNodes(route) {
  if (Array.isArray(route)) return route.map(node => typeof node === 'string' ? node : node?.id).filter(Boolean)
  if (route && typeof route === 'object') return routeNodes(route.path || route.nodes || route.route || [])
  return []
}
