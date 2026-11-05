import { useCallback, useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'

const PREFIX = 'route-studio:'

export function readStored(key, fallback) {
  try {
    const raw = window.localStorage.getItem(PREFIX + key)
    return raw == null ? fallback : JSON.parse(raw)
  } catch { return fallback }
}

export function writeStored(key, value) {
  try { window.localStorage.setItem(PREFIX + key, JSON.stringify(value)) } catch { /* private mode / quota */ }
}

/** useState that survives reloads and tab switches (localStorage). */
export function usePersistentState(key, initial) {
  const [value, setValue] = useState(() => readStored(key, typeof initial === 'function' ? initial() : initial))
  const first = useRef(true)
  useEffect(() => {
    if (first.current) { first.current = false; return }
    writeStored(key, value)
  }, [key, value])
  return [value, setValue]
}

/**
 * Shareable URL state: a few search params kept in sync with the address
 * bar (history.replace, so Back still leaves the page).
 */
export function useUrlState(defaults) {
  const [params, setParams] = useSearchParams()
  const state = {}
  for (const [key, fallback] of Object.entries(defaults)) {
    const raw = params.get(key)
    state[key] = raw == null ? fallback : typeof fallback === 'number' ? (Number.isFinite(+raw) ? +raw : fallback) : raw
  }
  const update = useCallback(patch => {
    setParams(previous => {
      const next = new URLSearchParams(previous)
      for (const [key, value] of Object.entries(patch)) {
        if (value === '' || value == null || value === defaults[key]) next.delete(key)
        else next.set(key, String(value))
      }
      return next
    }, { replace: true })
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [setParams])
  return [state, update, params]
}

/** base64url JSON for share links. */
export function encodeShare(value) {
  const bytes = new TextEncoder().encode(JSON.stringify(value))
  let binary = ''
  bytes.forEach(b => { binary += String.fromCharCode(b) })
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

export function decodeShare(text) {
  try {
    const padded = text.replace(/-/g, '+').replace(/_/g, '/') + '==='.slice((text.length + 3) % 4)
    const binary = atob(padded)
    const bytes = Uint8Array.from(binary, c => c.charCodeAt(0))
    return JSON.parse(new TextDecoder().decode(bytes))
  } catch { return null }
}

export const prefersReducedMotion = () => {
  try { return window.matchMedia('(prefers-reduced-motion: reduce)').matches } catch { return false }
}

/** Scroll a freshly rendered result section into view and move focus to it. */
export function revealResults(ref) {
  requestAnimationFrame(() => requestAnimationFrame(() => {
    const el = ref.current
    if (!el) return
    el.scrollIntoView({ behavior: prefersReducedMotion() ? 'auto' : 'smooth', block: 'start' })
    el.focus({ preventScroll: true })
  }))
}
