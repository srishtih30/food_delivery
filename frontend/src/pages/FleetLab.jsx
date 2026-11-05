import { memo, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { ArrowRight, Clock3, Gauge, Info, Pause, Play, Plus, RotateCcw, Shuffle, Sparkles, Timer, Trash2, Truck, Users, X } from 'lucide-react'
import { decimal, kilometers, minutes, nameFor, number, query, useRequest, useResource } from '../api'
import MapView from '../components/MapView'
import { Badge, BusyBar, Callout, Comparison, Empty, ErrorNote, PageHeading, Panel, SectionTitle, Segmented, SelectField, Stat, entityOptions } from '../components/Shared'
import { createClock, useClockValue, usePlayback } from '../lib/clock'
import { graphIndex, measure, nodePoint, pointAt, routeColor } from '../lib/geo'
import { readStored, revealResults, useUrlState, writeStored } from '../lib/persist'

const MODES = { direct: 'Direct', relay: 'Relay' }
const timeOf = value => value == null || !Number.isFinite(+value) ? '—' : `${decimal(value)} min`

/** Street polylines for every schedule segment, measured once per run. */
function buildTracks(drivers, index, orders) {
  return (drivers || []).map((driver, i) => ({
    id: driver.id,
    color: routeColor(i),
    start: nodePoint(index, driver.start),
    orders: orders || [],
    segs: (driver.segs || []).map(([t0, t1, a, b, carrying, orders]) => ({ t0: +t0, t1: +t1, a, b, carrying: Boolean(carrying), orders: orders || [], m: measure(index.hop(a, b)) })).sort((a, b) => a.t0 - b.t0),
  }))
}

function positionAt(track, t) {
  const carrying = track.orders.some(o => (o.a === track.id && t >= o.pickup && t < (o.h ? o.ht + 2 : o.delivered)) || (o.h && o.b === track.id && t >= o.ht + 2 && t < o.delivered))
  const segs = track.segs
  if (!segs.length || t < segs[0].t0) return { p: track.start, carrying }
  let lo = 0, hi = segs.length - 1
  while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (segs[mid].t0 <= t) lo = mid; else hi = mid - 1 }
  const s = segs[lo]
  if (t >= s.t1) return { p: s.m.line.at(-1) || track.start, carrying }
  const f = s.t1 > s.t0 ? (t - s.t0) / (s.t1 - s.t0) : 1
  return { p: pointAt(s.m, f), carrying }
}

/** Driver markers: positioned by direct DOM writes on every clock tick. */
const DriverLayer = memo(function DriverLayer({ tracks, clock, k, focus, relevant, handoffs }) {
  const refs = useRef([])
  useLayoutEffect(() => {
    const paint = t => {
      tracks.forEach((track, i) => {
        const el = refs.current[i]
        if (!el) return
        const { p, carrying } = positionAt(track, t)
        if (!p) return
        const meeting = handoffs.find(h => t >= h.t0 && t < h.t1 && (h.a === track.id || h.b === track.id))
        const offset = meeting ? (meeting.a === track.id ? -20 : 20) * k : 0
        el.setAttribute('transform', `translate(${p[0] + offset} ${p[1]}) scale(${k})`)
        const next = track.segs.find(s => s.t1 > t)
        el.querySelector('.driver-label').textContent = meeting ? `${track.id}: ${meeting.a === track.id ? 'giving' : 'receiving'}` : `${track.id}${next && t >= next.t0 ? ` to ${next.b}` : ' (waiting)'}`
        el.classList.toggle('carrying', carrying)
      })
    }
    paint(clock.get())
    return clock.subscribe(paint)
  }, [tracks, clock, k, handoffs])
  return <g className="driver-layer">
    {tracks.map((track, i) => <g key={track.id} ref={el => { refs.current[i] = el }} className={`driver-marker ${(focus && focus !== track.id) || (relevant && !relevant.includes(track.id)) ? 'faded' : ''}`}>
      <circle r="12" className="driver-halo" />
      <circle r="9" fill={track.color} stroke="#fff" strokeWidth="2.2" />
      <circle r="3.2" className="driver-bag" />
      <text y="-14" textAnchor="middle" className="driver-label">{track.id.replace(/^D0*/, 'D')}</text>
      <title>{track.id}</title>
    </g>)}
  </g>
})

/** A diamond on every hand-off point; it pulses while the hand-off happens. */
const HandoffLayer = memo(function HandoffLayer({ points, clock, k, selectedOrder }) {
  const refs = useRef([])
  useLayoutEffect(() => {
    const paint = t => points.forEach((h, i) => {
      const el = refs.current[i]
      if (!el) return
      el.classList.toggle('active', t >= h.t0 && t < h.t1)
      el.querySelector('.transfer-bag').setAttribute('transform', `translate(${-20 + 40 * Math.max(0, Math.min(1, (t - h.t0) / 2))} -24)`)
    })
    paint(clock.get())
    return clock.subscribe(paint)
  }, [points, clock])
  return <g className="handoff-layer">
    {points.map((h, i) => <g key={h.id} ref={el => { refs.current[i] = el }} className={`handoff-marker ${selectedOrder === String(h.id) ? "selected" : ""}`} transform={`translate(${h.p[0]} ${h.p[1]}) scale(${k})`}>
      <circle r="18" className="handoff-pulse" />
      <path d="M0 -11 L11 0 L0 11 L-11 0Z" className="handoff-diamond" />
      <text y="3.8" textAnchor="middle" className="handoff-text">H</text>
      <g className="transfer-detail"><path d="M-20 -24 H20" stroke="var(--amber)" strokeWidth="3" /><g className="transfer-bag"><rect x="-6" y="-6" width="12" height="12" rx="2" fill="var(--restaurant)" stroke="white" /></g><text y="-42" textAnchor="middle" className="transfer-label">{h.a} gives to {h.b}: {h.id}</text></g><title>{h.title}</title>
    </g>)}
  </g>
})

function OrderReplayStatus({ order, clock }) {
  const t = useClockValue(clock, 0.1)
  if (!order) return null
  let status
  if (t < order.created) status = 'Order has not been placed yet.'
  else if (t < order.pickup) status = `${order.a} is heading to pickup or waiting for food.`
  else if (t >= order.delivered) status = `Delivered by ${order.b}.`
  else if (!order.h) status = `${order.a} is carrying the food to the customer.`
  else if (t < order.ht) status = `${order.a} is bringing the food to the hand-off point. ${order.b} will meet them there.`
  else if (t < order.ht + 2) status = `Hand-off in progress: ${order.a} gives the food to ${order.b}. Both drivers stop for the transfer.`
  else status = `${order.b} now carries the food to the customer. ${order.a} is released and waits or heads to their next assigned pickup.`
  return <div className="callout" role="status"><strong>{order.id}</strong> · {status}</div>
}

function ClockReadout({ clock, max, onSeek }) {
  const t = useClockValue(clock, 0.1)
  return <div className="playback-range"><label htmlFor="fleet-time">Simulation clock <strong>{timeOf(t)}</strong> of {timeOf(max)}</label>
    <input id="fleet-time" type="range" min="0" max={max} step="0.1" value={Math.min(t, max)} onChange={e => onSeek(+e.target.value)} /></div>
}

function TimelineCursor({ clock, max }) {
  const ref = useRef(null)
  useLayoutEffect(() => {
    const paint = t => { if (ref.current) ref.current.style.left = `${(t / max) * 100}%` }
    paint(clock.get())
    return clock.subscribe(paint)
  }, [clock, max])
  return <span ref={ref} className="timeline-now" aria-hidden="true" />
}

const Timeline = memo(function Timeline({ tracks, jobs, maxTime, clock, focus, setFocus, onSeek }) {
  return <div className="timeline-scroll"><div className="timeline">
    <div className="timeline-axis"><span>Driver</span><div><span>0</span><span>{timeOf(maxTime / 4)}</span><span>{timeOf(maxTime / 2)}</span><span>{timeOf((maxTime * 3) / 4)}</span><span>{timeOf(maxTime)}</span></div><span /></div>
    <div className="timeline-body">
      {tracks.map(track => <div className={`timeline-row ${focus === track.id ? 'chosen' : ''} ${focus && focus !== track.id ? 'dim' : ''}`} key={track.id}>
        <button type="button" className="timeline-name" onClick={() => setFocus(focus === track.id ? '' : track.id)} aria-pressed={focus === track.id}><i className="swatch" style={{ background: track.color }} />{track.id}</button>
        <div className="timeline-track" onClick={e => { const r = e.currentTarget.getBoundingClientRect(); onSeek(((e.clientX - r.left) / r.width) * maxTime) }}>
          {track.segs.map((seg, i) => <span key={i} className={`timeline-seg ${seg.carrying ? 'carrying' : 'empty-run'}`}
            style={{ left: `${(seg.t0 / maxTime) * 100}%`, width: `${Math.max(0.15, ((seg.t1 - seg.t0) / maxTime) * 100)}%`, background: seg.carrying ? track.color : undefined, borderColor: track.color }}
            title={`${seg.a} → ${seg.b}, ${timeOf(seg.t0)} to ${timeOf(seg.t1)}${seg.carrying ? ', carrying ' : ', heading to '}${seg.orders.join(', ')}`} />)}
        </div>
        <span className="timeline-jobs">{jobs[track.id] ?? 0} jobs</span>
      </div>)}
      <div className="timeline-cursor-lane"><TimelineCursor clock={clock} max={maxTime} /></div>
    </div>
  </div></div>
})

/** Two overlapping histograms of delivery minutes. */
function DeliveryHistogram({ direct, relay }) {
  const all = [...direct, ...relay]
  if (!all.length) return null
  const max = Math.max(...all), binW = Math.max(5, Math.ceil(max / 12 / 5) * 5), bins = Math.ceil((max + 0.01) / binW)
  const count = list => Array.from({ length: bins }, (_, b) => list.filter(v => v >= b * binW && v < (b + 1) * binW).length)
  const d = count(direct), r = count(relay), top = Math.max(1, ...d, ...r)
  const W = 560, H = 170, pad = 26, bw = (W - pad * 2) / bins
  return <svg viewBox={`0 0 ${W} ${H + 24}`} className="histogram" role="img" aria-label="Delivery time distribution for direct and relay strategies">
    {[0, 0.5, 1].map(f => <g key={f}><line x1={pad} x2={W - pad} y1={H - f * (H - 20)} y2={H - f * (H - 20)} className="grid" /><text x={pad - 6} y={H - f * (H - 20) + 4} textAnchor="end" className="axis">{Math.round(f * top)}</text></g>)}
    {d.map((v, b) => <rect key={`d${b}`} x={pad + b * bw + 2} y={H - (v / top) * (H - 20)} width={bw / 2 - 2} height={(v / top) * (H - 20)} rx="2" fill="#0f8f8a"><title>Direct: {v} orders in {b * binW}–{(b + 1) * binW} min</title></rect>)}
    {r.map((v, b) => <rect key={`r${b}`} x={pad + b * bw + bw / 2} y={H - (v / top) * (H - 20)} width={bw / 2 - 2} height={(v / top) * (H - 20)} rx="2" fill="#d9480f"><title>Relay: {v} orders in {b * binW}–{(b + 1) * binW} min</title></rect>)}
    {Array.from({ length: bins + 1 }, (_, b) => b % 2 === 0 && <text key={b} x={pad + b * bw} y={H + 16} textAnchor="middle" className="axis">{b * binW}</text>)}
  </svg>
}

/** Ready-to-run defaults: two drivers meet; the sender stops and the receiver delivers. */
const SHOWCASE = {
  drivers: 10, orders: 0, seed: 3,
  ownDrivers: ['R017', 'R170'],
  ownOrders: [{ r: 'R017', s: 'S002', t: 0 }, { r: 'R170', s: 'S002', t: 3 }],
}
const RANDOM_FLEET = { drivers: 10, orders: 40, seed: 3, ownDrivers: [], ownOrders: [] }
const MAX_OWN_ORDERS = 6, MAX_OWN_DRIVERS = 12

function cleanFleet(raw) {
  if (!raw || typeof raw !== 'object') return SHOWCASE
  // Upgrade only the old built-in demo; preserve user-created scenarios.
  const oldDemo = [{ r: 'R017', s: 'S002', t: 0 }, { r: 'R170', s: 'S002', t: 3 }, { r: 'R079', s: 'S002', t: 6 }]
  if (+raw.orders === 0 && +raw.seed === 3
      && JSON.stringify(raw.ownDrivers) === JSON.stringify(SHOWCASE.ownDrivers)
      && JSON.stringify(raw.ownOrders) === JSON.stringify(oldDemo)) return SHOWCASE
  const num = (v, fallback) => v !== '' && v != null && Number.isFinite(+v) ? +v : fallback
  return {
    drivers: num(raw.drivers, 10), orders: num(raw.orders, 0), seed: num(raw.seed, 3),
    ownDrivers: Array.isArray(raw.ownDrivers) ? raw.ownDrivers.filter(d => typeof d === 'string').slice(0, MAX_OWN_DRIVERS) : [],
    ownOrders: Array.isArray(raw.ownOrders) ? raw.ownOrders.filter(o => o && o.r && o.s).map(o => ({ r: o.r, s: o.s, t: num(o.t, 0) })).slice(0, MAX_OWN_ORDERS) : [],
  }
}

export default function FleetLab({ catalog, graph }) {
  const [url, setUrl, params] = useUrlState({ mode: 'relay', run: '' })
  const [scn, setScn] = useState(() => {
    const legacy = ['drivers', 'orders', 'seed'].every(key => params.get(key) != null && Number.isFinite(+params.get(key)))
    return legacy ? { ...RANDOM_FLEET, drivers: +params.get('drivers'), orders: +params.get('orders'), seed: +params.get('seed') } : cleanFleet(readStored('fleet.v2', null))
  })
  const { drivers, orders, seed, ownDrivers, ownOrders } = scn
  const patch = change => setScn(s => ({ ...s, ...change }))
  useEffect(() => { writeStored('fleet.v2', scn) }, [scn])
  const [newDriver, setNewDriver] = useState(''), [newRestaurant, setNewRestaurant] = useState(''), [newSociety, setNewSociety] = useState(''), [newMinute, setNewMinute] = useState(0)
  const [data, setData] = useState(null)
  const [focusDriver, setFocusDriver] = useState(''), [selectedOrder, setSelectedOrder] = useState(''), [tab, setTab] = useState('order')
  const [rate, setRate] = useState(2), [trails, setTrails] = useState(true)
  const mode = MODES[url.mode] ? url.mode : 'relay'
  const request = useRequest({ timeout: 45000 })
  const resultsRef = useRef(null), mapRef = useRef(null)
  const index = useMemo(() => graphIndex(graph), [graph])
  const clock = useMemo(() => createClock(0), [])

  const pairCheck = useResource(newRestaurant && newSociety ? query('/serviceability', { restaurant: newRestaurant, society: newSociety }) : null)
  const pairBlocked = Boolean(pairCheck.data && !pairCheck.data.serviceable)
  const minuteInvalid = newMinute === '' || !Number.isFinite(+newMinute) || +newMinute < 0 || +newMinute > 120
  const canAddOrder = newRestaurant && newSociety && pairCheck.data?.serviceable && !minuteInvalid && ownOrders.length < MAX_OWN_ORDERS
  const canAddDriver = newDriver && ownDrivers.length < MAX_OWN_DRIVERS && !ownDrivers.includes(newDriver)

  const schedule = data?.[mode]
  const tracks = useMemo(() => schedule ? buildTracks(schedule.drivers, index, schedule.orders) : [], [schedule, index])
  const jobs = useMemo(() => Object.fromEntries((schedule?.drivers || []).map(d => [d.id, d.jobs])), [schedule])
  const maxTime = useMemo(() => {
    const ends = ['direct', 'relay'].flatMap(key => (data?.[key]?.drivers || []).flatMap(item => (item.segs || []).map(seg => +seg[1] || 0)))
    return Math.max(1, ...ends)
  }, [data])
  const [playing, setPlaying] = usePlayback(clock, maxTime, rate)
  const seek = t => { clock.set(Math.max(0, Math.min(maxTime, t))); setPlaying(false) }

  const randomDriversUsed = ownDrivers.length === 0
  const noOrders = !Number.isInteger(+orders) || +orders + ownOrders.length < 1
  const invalid = (randomDriversUsed && (!Number.isInteger(+drivers) || +drivers < 1 || +drivers > 80))
    || +orders < 0 || +orders > 100 || noOrders
    || !Number.isInteger(+seed) || +seed < 0 || +seed > 1000000
  const invalidReason = invalid ? (+orders >= 0 && +orders <= 100 && Number.isInteger(+orders) && +orders + ownOrders.length < 1 ? 'Add at least one order (random or your own).' : 'Check drivers (1–80), orders (0–100) and seed.') : ''

  async function run(event, opts = {}) {
    event?.preventDefault()
    if (invalid) return
    setPlaying(false)
    const result = await request.run(query('/fleet', {
      drivers: randomDriversUsed ? drivers : ownDrivers.length, orders, seed,
      driver: ownDrivers, custom: ownOrders.map(o => `${o.r},${o.s},${o.t}`),
    }))
    if (!result) return
    setData(result)
    clock.set(0)
    const firstRelay = result.relay?.orders?.find(item => item.h)
    setFocusDriver(''); setSelectedOrder(firstRelay ? String(firstRelay.id) : result.custom?.[0] || '')
    setUrl({ run: '1' })
    if (!opts.silent) revealResults(resultsRef)
  }
  // The defaults are ready to run, so their result shows straight away.
  useEffect(() => { run(null, { silent: true }) }, []) // eslint-disable-line react-hooks/exhaustive-deps

  function addOrder() {
    if (!canAddOrder) return
    patch({ ownOrders: [...ownOrders, { r: newRestaurant, s: newSociety, t: +newMinute }] })
    setNewRestaurant(''); setNewSociety(''); setNewMinute(0)
  }

  const order = schedule?.orders?.find(item => String(item.id) === selectedOrder)
  const otherOrder = data?.[mode === 'direct' ? 'relay' : 'direct']?.orders?.find(item => String(item.id) === selectedOrder)
  const relayOrders = useMemo(() => (data?.relay?.orders || []).filter(item => item.h), [data])
  const handoffPoints = useMemo(() => mode !== 'relay' ? [] : relayOrders.map(o => ({
    id: o.id, p: nodePoint(index, o.h), t0: o.ht, t1: o.ht + 2, a: o.a, b: o.b,
    title: `Hand-off of ${o.id}: ${o.a} gives the food to ${o.b} at ${nameFor(o.h, graph.nodes)}, minute ${decimal(o.ht)}`,
  })).filter(h => h.p), [mode, relayOrders, index, graph.nodes])

  function watch(item) {
    setUrl({ mode: 'relay' }); setTab('order'); setSelectedOrder(String(item.id)); setFocusDriver('')
    setRate(0.5); clock.set(Math.max(0, item.ht - 2)); setPlaying(true)
    mapRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }

  // Map layers -------------------------------------------------------
  const orderRoutes = useMemo(() => {
    if (!order) return []
    const out = []
    tracks.forEach(track => {
      const mine = track.segs.filter(s => s.orders.includes(order.id))
      const toPickup = mine.filter(s => !s.carrying), carrying = mine.filter(s => s.carrying)
      if (toPickup.length) out.push({ id: `${track.id}-p`, points: toPickup.flatMap(s => s.m.line), color: track.color, dashed: true, width: 4, title: `${track.id} heading to pickup` })
      if (carrying.length) out.push({ id: `${track.id}-c`, points: carrying.flatMap(s => s.m.line), color: track.color, width: 6, title: `${track.id} carrying ${order.id}` })
    })
    return out
  }, [order, tracks])
  const trailRoutes = useMemo(() => {
    if (order) return []
    const shown = focusDriver ? tracks.filter(t => t.id === focusDriver) : trails && tracks.length <= 30 ? tracks : []
    return shown.map(t => ({ id: `t-${t.id}`, points: t.segs.flatMap(s => s.m.line), color: t.color, width: focusDriver ? 5 : 3, ghost: !focusDriver, animate: Boolean(focusDriver) }))
  }, [order, tracks, focusDriver, trails])
  const pins = order ? [
    { id: 'pin-p', node: order.r, label: 'P', color: 'var(--restaurant)', title: `Pickup: ${nameFor(order.r, graph.nodes)}` },
    { id: 'pin-d', node: order.s, label: 'D', color: 'var(--society)', title: `Drop-off: ${nameFor(order.s, graph.nodes)}` },
  ] : []
  const fitTarget = order ? orderRoutes.flatMap(r => r.points) : focusDriver ? trailRoutes.flatMap(r => r.points) : null
  const driverDetail = focusDriver ? schedule?.drivers?.find(d => d.id === focusDriver) : null
  const driverOrders = focusDriver ? (schedule?.orders || []).filter(o => o.a === focusDriver || o.b === focusDriver) : []
  const nodeOptions = useMemo(() => entityOptions(graph.nodes).filter(option => !ownDrivers.includes(option.value)), [graph.nodes, ownDrivers])

  return <div className="page-stack">
    <PageHeading title="Relay hand-offs: one driver passes the food on"
      description="When two drivers are heading the same way, the first hands the food over mid-route and goes back to the restaurants, while the second carries on to the customer. The defaults below set exactly that up; change anything and run again."
      action={<div className="heading-buttons">
        <button type="button" className="button secondary" onClick={() => setScn(SHOWCASE)}><Sparkles size={16} /> Reset to relay showcase</button>
        <button type="button" className="button secondary" onClick={() => setScn(RANDOM_FLEET)}><Shuffle size={16} /> Random fleet</button>
      </div>} />

    <Panel className="panel-pad">
      <form onSubmit={run}>
        <div className="fleet-controls">
          <div className="field"><label htmlFor="fleet-drivers">Random drivers (1–80)</label><input id="fleet-drivers" type="number" min="1" max="80" disabled={!randomDriversUsed} required={randomDriversUsed} value={drivers} onChange={e => patch({ drivers: e.target.value })} /></div>
          <div className="field"><label htmlFor="fleet-orders">Random orders (0–100)</label><input id="fleet-orders" type="number" min="0" max="100" required value={orders} onChange={e => patch({ orders: e.target.value })} /></div>
          <div className="field"><label htmlFor="fleet-seed">Random seed</label><input id="fleet-seed" type="number" min="0" max="1000000" step="1" required value={seed} onChange={e => patch({ seed: e.target.value })} /></div>
          <button className="button primary" disabled={request.busy || invalid} type="submit"><Play size={17} />{request.busy ? 'Simulating…' : 'Run simulation'}</button>
        </div>
        {invalidReason && <p className="field-error">{invalidReason}</p>}

        <div className="two-col fleet-own">
          <div className="own-block page-stack">
            <h3 className="small-title">Your drivers <span className="optional">replace the random ones</span></h3>
            <div className="inline-builder">
              <SelectField id="fleet-driver" label="Starting place" value={newDriver} onChange={setNewDriver} options={nodeOptions} placeholder="Choose a place" />
              <button className="button secondary" type="button" disabled={!canAddDriver} onClick={() => { patch({ ownDrivers: [...ownDrivers, newDriver] }); setNewDriver('') }}><Plus size={17} /> Add</button>
            </div>
            <div className="driver-tags">{ownDrivers.map((id, i) => <span className="driver-tag" key={id}><Truck size={14} /> D{String(i + 1).padStart(3, '0')}: {nameFor(id, graph.nodes)} <button type="button" aria-label={`Remove driver ${i + 1}`} onClick={() => patch({ ownDrivers: ownDrivers.filter((_, j) => j !== i) })}><X size={13} /></button></span>)}
              {!ownDrivers.length && <p className="empty-list">None: {drivers} random drivers are used.</p>}</div>
          </div>
          <div className="own-block page-stack">
            <h3 className="small-title">Your orders <span className="optional">{ownOrders.length} of {MAX_OWN_ORDERS}</span></h3>
            <div className="planner-fields">
              <SelectField id="fleet-restaurant" label="Pickup restaurant" value={newRestaurant} onChange={setNewRestaurant} options={entityOptions(catalog.restaurants)} placeholder="Choose restaurant" />
              <SelectField id="fleet-society" label="Drop-off society" value={newSociety} onChange={setNewSociety} options={entityOptions(catalog.societies)} placeholder="Choose society" />
              <div className="field"><label htmlFor="fleet-created">Placed at minute (0–120)</label><input id="fleet-created" type="number" min="0" max="120" step="any" value={newMinute} onChange={e => setNewMinute(e.target.value)} /></div>
              <div className="field add-field"><button className="button secondary" type="button" disabled={!canAddOrder} onClick={addOrder}><Plus size={17} /> Add order</button></div>
            </div>
            {pairBlocked && <p className="field-error">This pair is blocked by policy: {pairCheck.data.message}. Choose a restaurant in the same or an allowed zone.</p>}
            {ownOrders.length > 0 ? <ol className="queue">{ownOrders.map((o, i) => <li className="builder-item" key={i}>
              <span className="item-index" style={{ background: routeColor(i) }}>{i + 1}</span>
              <div><strong>{nameFor(o.r, graph.nodes)} <ArrowRight size={13} /> {nameFor(o.s, graph.nodes)}</strong><small>placed at minute {o.t}</small></div>
              <button type="button" className="icon-button remove" aria-label={`Remove order ${i + 1}`} onClick={() => patch({ ownOrders: ownOrders.filter((_, j) => j !== i) })}><Trash2 size={15} /></button>
            </li>)}</ol> : <p className="empty-list">None: only random orders are simulated.</p>}
          </div>
        </div>
      </form>
      <BusyBar busy={request.busy} startedAt={request.startedAt} onCancel={request.cancel} label="Simulating both strategies" />
      <ErrorNote message={request.error} onDismiss={request.clearError} />
    </Panel>

    <section ref={resultsRef} tabIndex={-1} className="results-anchor page-stack" aria-label="Fleet simulation results">
      {!data ? <Panel><Empty icon={Truck} title="Nothing simulated yet" description="Run a simulation to animate the fleet. Each seed gives the same orders and drivers every time, so runs are comparable and shareable." /></Panel> : <>
        <div className="run-summary"><Badge kind="info">{ownDrivers.length || ownOrders.length ? 'Your scenario' : `Seed ${data.seed}`}</Badge><span><Users size={16} />{data.drivers} drivers</span><span><Gauge size={16} />{(schedule?.orders || []).length} orders</span>{data.custom?.length > 0 && <Badge kind="neutral">Your orders: {data.custom.join(', ')}</Badge>}</div>
        <div className="stats-row four">
          <Stat label="Relay hand-offs" value={number(data.relay?.metrics?.relays)} note="Orders passed between drivers" icon={ArrowRight} tone="amber" emphasis />
          <Stat label="Average delivery, direct" value={decimal(data.direct?.metrics?.avg_delivery_min)} unit="min" note={`90th percentile ${timeOf(data.direct?.metrics?.p90_delivery_min)}`} icon={Clock3} />
          <Stat label="Average delivery, relay" value={decimal(data.relay?.metrics?.avg_delivery_min)} unit="min" note={`90th percentile ${timeOf(data.relay?.metrics?.p90_delivery_min)}`} icon={Timer} tone="chili" />
          <Stat label="Empty driving saved" value={decimal((data.direct?.metrics?.empty_km || 0) - (data.relay?.metrics?.empty_km || 0))} unit="km" note="Direct minus relay" icon={Truck} tone="violet" />
        </div>

        <Panel className="panel-pad handoff-panel">
          <SectionTitle title="Hand-offs in this run" description="Driver A collects the food and carries it along the shared road; driver B, already heading that way, takes it at the meeting point and finishes the delivery." />
          {relayOrders.length ? <div className="handoff-cards">{relayOrders.map(o => {
            const extra = o.delivered - o.direct
            return <article key={o.id} className={`handoff-card ${selectedOrder === String(o.id) && mode === 'relay' ? 'active' : ''}`}>
              <div className="hc-head"><Badge kind="warning">{o.id}</Badge><span>meet at minute <strong>{decimal(o.ht)}</strong></span></div>
              <p className="hc-story"><strong>{o.a}</strong> picks up at {nameFor(o.r, graph.nodes)} and drives to <strong>{nameFor(o.h, graph.nodes)}</strong>. <strong>{o.b}</strong> takes the food there and delivers to {nameFor(o.s, graph.nodes)}.</p>
              <dl className="hc-facts">
                <div><dt>{o.a} freed</dt><dd>{decimal(o.freed)} min sooner</dd></div>
                <div><dt>{o.b} extra work</dt><dd>{decimal(o.helper)} min</dd></div>
                <div><dt>Customer</dt><dd>{extra > 0.05 ? `+${decimal(extra)} min` : extra < -0.05 ? `${decimal(extra)} min` : 'no change'}</dd></div>
              </dl>
              <button type="button" className="button secondary small" onClick={() => watch(o)}><Play size={14} /> Watch this hand-off</button>
            </article>
          })}</div> : <Empty icon={Info} title="No hand-off happened in this run" description="A hand-off needs a long trip (15+ minutes) and a second driver already passing a shared point at the right time. Reset to the relay showcase, or try another seed or fleet size." />}
        </Panel>

        <div className="fleet-layout" ref={mapRef}>
          <Panel className="map-panel">
            <div className="map-panel-head">
              <Segmented label="Strategy" value={mode} onChange={value => { setUrl({ mode: value }); setFocusDriver('') }} options={Object.entries(MODES).map(([value, label]) => ({ value, label }))} />
              <label className="check"><input type="checkbox" checked={trails} onChange={e => setTrails(e.target.checked)} /> Driver trails</label>
            </div>
            <MapView graph={graph} className="tall" routes={[...trailRoutes, ...orderRoutes]} pins={pins} fit={fitTarget?.length ? fitTarget : null}
              fitKey={order ? `o-${order.id}-${mode}` : focusDriver ? `d-${focusDriver}-${mode}` : 'all'}
              highlight={order ? [order.r, order.s, order.h] : []}
              legend={[{ type: 'dot', color: '#5b4bd6', label: 'Driver, one colour each' }, { type: 'ring', color: 'var(--restaurant)', label: 'Orange halo: carrying food' }, ...(mode === 'relay' ? [{ type: 'dot', color: '#c2891a', label: 'H: hand-off point' }] : []), ...(order ? [{ type: 'line', color: '#555', dashed: true, label: 'To pickup' }, { type: 'line', color: '#555', label: 'Carrying' }] : [])]}
              overlay={k => <><HandoffLayer points={handoffPoints} clock={clock} k={k} selectedOrder={selectedOrder} /><DriverLayer tracks={tracks} clock={clock} k={k} focus={focusDriver} handoffs={handoffPoints} relevant={order ? [order.a, order.b].filter(Boolean) : null} /></>} />
            <OrderReplayStatus order={order} clock={clock} />
            <div className="playback fleet-playback">
              <button className="button primary play-button" onClick={() => { if (clock.get() >= maxTime) clock.set(0); setPlaying(!playing) }} aria-label={playing ? 'Pause fleet replay' : 'Play fleet replay'}>{playing ? <Pause size={17} /> : <Play size={17} />}</button>
              <button className="icon-button" aria-label="Restart fleet replay" onClick={() => seek(0)}><RotateCcw size={17} /></button>
              <ClockReadout clock={clock} max={maxTime} onSeek={seek} />
              <select value={rate} aria-label="Replay speed" onChange={e => setRate(+e.target.value)}><option value="0.5">0.5 min/s (hand-off detail)</option><option value="2">2 min/s</option><option value="6">6 min/s</option><option value="12">12 min/s</option><option value="25">25 min/s</option></select>
            </div>
          </Panel>

          <Panel className="panel-pad fleet-inspector">
            <Segmented label="Inspect" value={tab} onChange={setTab} options={[{ value: 'order', label: 'Follow an order' }, { value: 'driver', label: 'Follow a driver' }]} />
            {tab === 'order' ? <>
              <div className="field"><label htmlFor="fleet-order">Order</label><select id="fleet-order" value={selectedOrder} onChange={e => { setSelectedOrder(e.target.value); setFocusDriver('') }}><option value="">All orders</option>{(schedule?.orders || []).map(item => <option key={item.id} value={String(item.id)}>{item.id}{item.h ? ' (relay)' : ''}: {nameFor(item.r, graph.nodes)} to {nameFor(item.s, graph.nodes)}</option>)}</select></div>
              {order ? <div className="order-detail">
                <div className="order-endpoints"><span className="pin-chip r">P</span><strong>{nameFor(order.r, graph.nodes)}</strong><span className="pin-chip s">D</span><strong>{nameFor(order.s, graph.nodes)}</strong></div>
                <ol className="milestones">{[['Placed', order.created], ['Food ready', order.ready], [`Picked up by ${order.a}`, order.pickup], ...(order.h ? [[`Handed to ${order.b} at ${nameFor(order.h, graph.nodes)}`, order.ht]] : []), [`Delivered by ${order.b}`, order.delivered]].filter(([, t]) => t != null).map(([name, value]) => <li key={name}><span>{name}</span><strong>{timeOf(value)}</strong></li>)}</ol>
                <div className="order-compare"><div><span>Direct</span><strong>{timeOf(mode === 'direct' ? order.delivered - order.created : otherOrder && otherOrder.delivered - otherOrder.created)}</strong></div><div><span>Relay</span><strong>{timeOf(mode === 'relay' ? order.delivered - order.created : otherOrder && otherOrder.delivered - otherOrder.created)}</strong></div></div>
                <button type="button" className="button secondary small" onClick={() => seek(Math.max(0, order.created - 1))}>Jump to when it was placed</button>
              </div> : <Empty icon={Info} title="Pick an order" description="Its pickup leg (dashed) and delivery leg (solid) are drawn in the colour of each driver who carried it." />}
            </> : <>
              <div className="driver-chips">{tracks.map(t => <button key={t.id} type="button" className={`driver-chip ${focusDriver === t.id ? 'active' : ''}`} onClick={() => { setFocusDriver(focusDriver === t.id ? '' : t.id); setSelectedOrder('') }} aria-pressed={focusDriver === t.id}><i className="swatch" style={{ background: t.color }} />{t.id}</button>)}</div>
              {driverDetail ? <div className="order-detail">
                <div className="detail-grid"><div><span>Distance</span><strong>{kilometers(driverDetail.km, 1)}</strong></div><div><span>Jobs</span><strong>{driverDetail.jobs}</strong></div><div><span>Starts at</span><strong>{nameFor(driverDetail.start, graph.nodes)}</strong></div><div><span>Segments</span><strong>{driverDetail.segs.length}</strong></div></div>
                <h3 className="small-title">Orders handled</h3>
                <ul className="plain-list">{driverOrders.map(o => <li key={o.id}><button type="button" className="text-button" onClick={() => { setTab('order'); setSelectedOrder(o.id); setFocusDriver('') }}>{o.id}</button> {o.a === focusDriver && o.b === focusDriver ? 'picked up and delivered' : o.a === focusDriver ? `picked up, handed to ${o.b}` : `received from ${o.a}, delivered`}</li>)}{!driverOrders.length && <li className="muted">No orders in this strategy.</li>}</ul>
              </div> : <Empty icon={Truck} title="Pick a driver" description="Their whole route for the day is drawn on the map; other drivers fade." />}
            </>}
          </Panel>
        </div>

        <Panel>
          <div className="panel-pad"><SectionTitle title={`${MODES[mode]} schedule, minute by minute`} description="Solid bars carry food, outlined bars drive empty. Click a track to jump the clock; click a driver to follow them." /></div>
          <Timeline tracks={tracks} jobs={jobs} maxTime={maxTime} clock={clock} focus={focusDriver} setFocus={id => { setFocusDriver(id); setSelectedOrder(''); setTab('driver') }} onSeek={seek} />
        </Panel>

        <div className="two-col">
          <Panel>
            <div className="panel-pad"><SectionTitle title="Direct vs relay" description="Same drivers and orders. Lower is better for every row." /></div>
            <Comparison first="Direct" second="Relay" firstColor="#0f8f8a" secondColor="#d9480f" rows={[
              ['Average delivery', minutes(data.direct?.metrics?.avg_delivery_min), minutes(data.relay?.metrics?.avg_delivery_min), better(data.direct?.metrics?.avg_delivery_min, data.relay?.metrics?.avg_delivery_min)],
              ['90th percentile', minutes(data.direct?.metrics?.p90_delivery_min), minutes(data.relay?.metrics?.p90_delivery_min), better(data.direct?.metrics?.p90_delivery_min, data.relay?.metrics?.p90_delivery_min)],
              ['Slowest delivery', minutes(data.direct?.metrics?.max_delivery_min), minutes(data.relay?.metrics?.max_delivery_min), better(data.direct?.metrics?.max_delivery_min, data.relay?.metrics?.max_delivery_min)],
              ['Total driving', kilometers(data.direct?.metrics?.total_km, 1), kilometers(data.relay?.metrics?.total_km, 1), better(data.direct?.metrics?.total_km, data.relay?.metrics?.total_km)],
              ['Empty driving', kilometers(data.direct?.metrics?.empty_km, 1), kilometers(data.relay?.metrics?.empty_km, 1), better(data.direct?.metrics?.empty_km, data.relay?.metrics?.empty_km)],
              ['Longest carry', minutes(data.direct?.metrics?.longest_drive_min), minutes(data.relay?.metrics?.longest_drive_min), better(data.direct?.metrics?.longest_drive_min, data.relay?.metrics?.longest_drive_min)],
              ['Drivers used', number(data.direct?.metrics?.drivers_used), number(data.relay?.metrics?.drivers_used)],
            ]} />
          </Panel>
          <Panel className="panel-pad">
            <SectionTitle title="Delivery time per order" description="How many orders arrived within each band of minutes (x axis: minutes from order to door)." />
            <div className="chart-key"><span><i className="swatch" style={{ background: '#0f8f8a' }} />Direct</span><span><i className="swatch" style={{ background: '#d9480f' }} />Relay</span></div>
            <DeliveryHistogram direct={(data.direct?.orders || []).map(o => o.delivered - o.created)} relay={(data.relay?.orders || []).map(o => o.delivered - o.created)} />
          </Panel>
        </div>
        <Callout icon={Info}>A relay is a dispatch option, not automatically an improvement. It frees the first driver sooner (less empty driving, ready for the next order) while the hand-off can add a few minutes to that one order.</Callout>
      </>}
    </section>
  </div>
}

function better(a, b) {
  if (a == null || b == null || Math.abs(a - b) < 1e-9) return 0
  return a < b ? 1 : 2
}
