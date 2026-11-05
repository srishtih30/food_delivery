import { useEffect, useRef, useState, useSyncExternalStore } from 'react'

/**
 * A tiny external store for the playback clock. The animation loop writes
 * to it ~60 times a second; only subscribers that read it directly (the
 * driver markers, the timeline cursor) update, so the page itself does not
 * re-render every frame.
 */
export function createClock(initial = 0) {
  let time = initial
  const subscribers = new Set()
  return {
    get: () => time,
    set(value) {
      if (value === time) return
      time = value
      subscribers.forEach(fn => fn(time))
    },
    subscribe(fn) { subscribers.add(fn); return () => subscribers.delete(fn) },
  }
}

/** React value of the clock, rounded to `step` so readers re-render rarely. */
export function useClockValue(clock, step = 0.5) {
  return useSyncExternalStore(clock.subscribe, () => Math.round(clock.get() / step) * step)
}

/**
 * requestAnimationFrame playback. `rate` is simulator minutes per real
 * second. Stops at `max`.
 */
export function usePlayback(clock, max, rate) {
  const [playing, setPlaying] = useState(false)
  const rateRef = useRef(rate)
  rateRef.current = rate
  useEffect(() => {
    if (!playing) return
    let frame = 0
    let last = performance.now()
    const tick = now => {
      const dt = Math.min(0.1, (now - last) / 1000)
      last = now
      const next = Math.min(max, clock.get() + dt * rateRef.current)
      clock.set(next)
      if (next >= max) { setPlaying(false); return }
      frame = requestAnimationFrame(tick)
    }
    frame = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(frame)
  }, [playing, clock, max])
  return [playing, setPlaying]
}
