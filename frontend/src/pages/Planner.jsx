import { useEffect, useMemo, useRef, useState } from 'react'
import { AlertTriangle, ArrowRight, CheckCircle2, CircleHelp, Construction, Copy, GitCompareArrows, Plus, Route, ShieldAlert, Sparkles, Trash2, Truck, X } from 'lucide-react'
import { api, decimal, kilometers, minutes, nameFor, number, percent, query, useRequest, useResource } from '../api'
import MapView from '../components/MapView'
import { Badge, BusyBar, Callout, Empty, ErrorNote, PageHeading, Panel, RouteLegs, SectionTitle, Segmented, SelectField, entityOptions } from '../components/Shared'
import { graphIndex, isRestaurant, pathLegs, routeColor } from '../lib/geo'
import { decodeShare, encodeShare, readStored, revealResults, usePersistentState, writeStored } from '../lib/persist'
import { useSearchParams } from 'react-router-dom'

const MAX_ORDERS = 60, MAX_DRIVERS = 30, MAX_CLOSURES = 12
const DEFAULT_SETTINGS = { capacity: 3, algorithm: 'tree', metric: 'time', improve: 'relocate', maxRide: '20' }
const SAMPLE = {
  orders: [['R203', 'S002'], ['R097', 'S003'], ['R210', 'S004'], ['R181', 'S005'], ['R224', 'S007'], ['R231', 'S008'], ['R062', 'S009'], ['R175', 'S010']].map(([restaurant, society]) => ({ restaurant, society })),
  drivers: ['R100', 'S013', 'R030'],
  closures: [],
  settings: DEFAULT_SETTINGS,
}
const METHOD = { append: { name: 'Greedy append', short: 'Greedy' }, insertion: { name: 'Cheapest insertion', short: 'Insertion' } }
const STATUS_KIND = { DELIVERED: 'success', REJECTED: 'warning', FAILED: 'danger' }
const label = key => String(key).replaceAll('_', ' ').replace(/^\w/, c => c.toUpperCase())

function cleanScenario(raw) {
  const s = raw && typeof raw === 'object' ? raw : {}
  return {
    orders: Array.isArray(s.orders) ? s.orders.filter(o => o && o.restaurant && o.society).slice(0, MAX_ORDERS) : [],
    drivers: Array.isArray(s.drivers) ? s.drivers.filter(d => typeof d === 'string').slice(0, MAX_DRIVERS) : [],
    closures: Array.isArray(s.closures) ? s.closures.filter(c => c && c.source && c.target).slice(0, MAX_CLOSURES) : [],
    settings: { ...DEFAULT_SETTINGS, ...(s.settings || {}) },
  }
}

function payloadOf(scenario) {
  const { settings } = scenario
  return {
    orders: scenario.orders.map(({ restaurant, society, ready_at, deadline }) => ({ restaurant, society, ...(ready_at != null ? { ready_at } : {}), ...(deadline != null ? { deadline } : {}) })),
    drivers: scenario.drivers, capacity: +settings.capacity, algorithm: settings.algorithm, metric: settings.metric, improve: settings.improve,
    max_ride_extra_min: settings.maxRide === '' ? null : +settings.maxRide, closures: scenario.closures,
  }
}

/** Paired horizontal bars, greedy vs insertion. */
function TotalsBars({ append, insertion }) {
  const rows = [
    ['Distance driven', 'distance_km', v => kilometers(v, 1), true],
    ['Driving time', 'drive_time_min', v => minutes(v), true],
    ['Drivers used', 'drivers_used', v => number(v), true],
    ['Orders batched', 'batched', v => number(v), false],
    ['Delivered', 'delivered', v => number(v), false],
    ['Late', 'late', v => number(v), true],
  ]
  return <div className="totals-bars">{rows.map(([name, key, fmt, lowerBetter]) => {
    const a = +append?.[key] || 0, b = +insertion?.[key] || 0, max = Math.max(a, b, 1e-9)
    const win = a === b ? 0 : (lowerBetter ? (b < a ? 2 : 1) : (b > a ? 2 : 1))
    return <div className="tb-row" key={key}>
      <span className="tb-name">{name}</span>
      <div className="tb-bars">
        <div className={`tb-bar append ${win === 1 ? 'win' : ''}`}><span style={{ width: `${(a / max) * 100}%` }} /><em>{fmt(append?.[key])}</em></div>
        <div className={`tb-bar insertion ${win === 2 ? 'win' : ''}`}><span style={{ width: `${(b / max) * 100}%` }} /><em>{fmt(insertion?.[key])}</em></div>
      </div>
    </div>
  })}</div>
}

export default function Planner({ catalog, graph }) {
  const index = useMemo(() => graphIndex(graph), [graph])
  const [params, setParams] = useSearchParams()
  const [scenario, setScenario] = usePersistentState('planner.v2', () => cleanScenario(null))
  const { orders, drivers, closures, settings } = scenario
  const update = patch => setScenario(s => ({ ...s, ...(typeof patch === 'function' ? patch(s) : patch) }))
  const setSetting = (key, value) => update(s => ({ settings: { ...s.settings, [key]: value } }))

  const [restaurant, setRestaurant] = useState(''), [society, setSociety] = useState(''), [ready, setReady] = useState(''), [deadline, setDeadline] = useState('')
  const [driver, setDriver] = useState('')
  const [edgeKey, setEdgeKey] = useState(''), [closureAt, setClosureAt] = useState(0), [reopen, setReopen] = useState('')
  const [pickMode, setPickMode] = useState('pickup')
  const [result, setResult] = useState(null), [method, setMethod] = useState('insertion'), [focus, setFocus] = useState('')
  const [copied, setCopied] = useState(false), [autoRun, setAutoRun] = useState(false)
  const request = useRequest({ timeout: 45000 })
  const resultsRef = useRef(null)

  // Load a shared scenario from ?s=..., then drop it from the address bar.
  useEffect(() => {
    const shared = params.get('s')
    if (!shared) return
    const decoded = decodeShare(shared)
    if (decoded) { setScenario(cleanScenario(decoded)); setResult(null); if (params.get('go') === '1') setAutoRun(true) }
    setParams(p => { const n = new URLSearchParams(p); n.delete('s'); n.delete('go'); return n }, { replace: true })
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // Orders loaded from a share link or the sample have no route yet: fetch
  // each one's road path so the queue is drawn on the map too.
  useEffect(() => {
    const missing = orders.map((o, i) => [o, i]).filter(([o]) => !o.route)
    if (!missing.length) return
    const controller = new AbortController()
    Promise.all(missing.map(([o]) => api(query('/serviceability', { restaurant: o.restaurant, society: o.society }), { signal: controller.signal }).catch(() => null)))
      .then(results => {
        if (controller.signal.aborted) return
        update(s => ({ orders: s.orders.map(o => {
          const hit = results.find(r => r && r.restaurant === o.restaurant && r.society === o.society)
          return o.route || !hit ? o : { ...o, route: hit.route?.length ? hit.route : hit.preview?.route || [], km: hit.distance_km ?? hit.preview?.distance_km, min: hit.time_min ?? hit.preview?.time_min, blocked: !hit.serviceable }
        }) }))
      })
    return () => controller.abort()
  }, [orders.length, orders.filter(o => !o.route).length]) // eslint-disable-line react-hooks/exhaustive-deps

  // Pair check for the order being composed (also gives the route to draw).
  const check = useResource(restaurant && society ? query('/serviceability', { restaurant, society }) : null)
  const pair = check.data && check.data.restaurant === restaurant && check.data.society === society ? check.data : null
  const blocked = Boolean(pair && !pair.serviceable)
  const alternatives = useResource(blocked ? query('/coverage', { society }) : null)
  const suggestions = useMemo(() => (alternatives.data?.restaurants || []).filter(r => r.serviceable).sort((a, b) => a.distance_km - b.distance_km).slice(0, 4), [alternatives.data])

  const timesInvalid = (ready !== '' && (!Number.isFinite(+ready) || +ready < 0)) || (deadline !== '' && (!Number.isFinite(+deadline) || +deadline < 0)) || (ready !== '' && deadline !== '' && +deadline < +ready)
  const canAdd = restaurant && society && pair?.serviceable && !check.loading && !timesInvalid && orders.length < MAX_ORDERS
  const addReason = !restaurant || !society ? 'Choose a pickup and a drop-off first.' : check.loading || !pair ? 'Checking the delivery policy…' : blocked ? 'This pair is blocked by the delivery policy, so it cannot be added.' : timesInvalid ? 'Fix the ready time and deadline.' : orders.length >= MAX_ORDERS ? `The queue is full (${MAX_ORDERS}).` : ''

  function addOrder() {
    if (!canAdd) return
    update(s => ({ orders: [...s.orders, { restaurant, society, ...(ready !== '' ? { ready_at: +ready } : {}), ...(deadline !== '' ? { deadline: +deadline } : {}), route: pair.route, km: pair.distance_km, min: pair.time_min }] }))
    setRestaurant(''); setSociety(''); setReady(''); setDeadline(''); setPickMode('pickup')
  }

  const directedEdges = useMemo(() => (graph.edges || []).flatMap(edge => [edge.ab ? { source: edge.a, target: edge.b } : null, edge.ba ? { source: edge.b, target: edge.a } : null].filter(Boolean)), [graph])
  // Offer roads near the current scenario first: those on queued order routes.
  const edgeOptions = useMemo(() => {
    const onRoutes = new Set(orders.flatMap(o => (o.route || []).slice(1).map((n, i) => `${o.route[i]}>${n}`)))
    return directedEdges.map((edge, i) => ({ i, edge, used: onRoutes.has(`${edge.source}>${edge.target}`) }))
      .sort((a, b) => b.used - a.used)
      .map(({ i, edge, used }) => ({ value: String(i), label: `${used ? 'On a queued route: ' : ''}${nameFor(edge.source, graph.nodes)} → ${nameFor(edge.target, graph.nodes)}` }))
  }, [directedEdges, orders, graph.nodes])
  const closureInvalid = closureAt === '' || +closureAt < 0 || (reopen !== '' && +reopen <= +closureAt)
  function addClosure() {
    const edge = directedEdges[+edgeKey]
    if (!edge || closureInvalid) return
    if (closures.some(c => c.source === edge.source && c.target === edge.target)) return
    update(s => ({ closures: [...s.closures, { ...edge, at: +closureAt, ...(reopen !== '' ? { reopen_at: +reopen } : {}) }] }))
    setEdgeKey(''); setReopen('')
  }

  function onMapPick(node) {
    if (pickMode === 'pickup') { if (isRestaurant(node)) { setRestaurant(node.id); setPickMode('dropoff') } }
    else if (pickMode === 'dropoff') { if (!isRestaurant(node)) setSociety(node.id) }
    else if (pickMode === 'driver' && !drivers.includes(node.id) && drivers.length < MAX_DRIVERS) update(s => ({ drivers: [...s.drivers, node.id] }))
  }
  const pickable = pickMode === 'pickup' ? isRestaurant : pickMode === 'dropoff' ? (n => !isRestaurant(n)) : undefined

  const payload = useMemo(() => payloadOf(scenario), [scenario])
  const signature = JSON.stringify(payload)
  const capacityInvalid = !Number.isInteger(+settings.capacity) || +settings.capacity < 1 || +settings.capacity > 10
  const rideInvalid = settings.maxRide !== '' && (!Number.isFinite(+settings.maxRide) || +settings.maxRide < 0 || +settings.maxRide > 120)
  const canRun = orders.length > 0 && drivers.length > 0 && !capacityInvalid && !rideInvalid

  async function run(event, opts = {}) {
    event?.preventDefault()
    if (!canRun) return
    const data = await request.run('/plans', { method: 'POST', body: signature })
    if (!data) return
    setResult({ ...data, signature })
    setFocus('')
    writeStored('planner.lastRun', signature)
    if (!opts.silent) revealResults(resultsRef)
  }
  // Re-run the last comparison after a reload so results come back too.
  useEffect(() => { if (canRun && readStored('planner.lastRun', '') === signature) run(null, { silent: true }) }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // A sample opened from the home page runs as soon as its scenario is in place.
  useEffect(() => {
    if (!autoRun || !canRun) return
    setAutoRun(false)
    run(null)
  }) // eslint-disable-line react-hooks/exhaustive-deps

  function share() {
    const link = `${window.location.origin}${window.location.pathname}?s=${encodeShare(scenario)}`
    navigator.clipboard?.writeText(link).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1800) }).catch(() => window.prompt('Copy this link', link))
  }

  // -- composing map -------------------------------------------------
  const composeRoutes = [
    ...orders.map((o, i) => ({ id: `q${i}`, path: o.route || [], color: routeColor(i), width: 3, ghost: true })),
    ...(pair ? [{ id: 'draft', path: pair.route?.length ? pair.route : pair.preview?.route || [], color: pair.serviceable ? 'var(--route)' : 'var(--danger)', dashed: !pair.serviceable, width: 5.5 }] : []),
  ]
  const composePins = [
    ...orders.flatMap((o, i) => [{ id: `qp${i}`, node: o.restaurant, label: `${i + 1}`, color: routeColor(i) }]),
    ...drivers.map((d, i) => ({ id: `dr${i}`, node: d, label: `D${i + 1}`, color: 'var(--ink)', variant: 'truck' })),
    ...(restaurant ? [{ id: 'nr', node: restaurant, label: 'P', color: 'var(--restaurant)' }] : []),
    ...(society ? [{ id: 'ns', node: society, label: 'D', color: 'var(--society)' }] : []),
  ]
  const draftPath = pair ? (pair.route?.length ? pair.route : pair.preview?.route || []) : []

  // -- results map ---------------------------------------------------
  const report = result?.methods?.[method]
  const driverRoutes = useMemo(() => (report?.drivers || []).map((d, i) => ({ ...d, color: routeColor(i), legs: pathLegs(index, d.path) })), [report, index])
  const shownDrivers = focus ? driverRoutes.filter(d => d.driver_id === focus) : driverRoutes
  const resultRoutes = shownDrivers.map(d => ({ id: d.driver_id, path: d.path, color: d.color, width: focus ? 6 : 4.5, title: `${d.driver_id}: ${kilometers(d.distance_km, 1)}` }))
  const orderNo = id => String(+String(id).replace(/\D/g, '') || id)
  const resultPins = shownDrivers.flatMap(d => [
    { id: `${d.driver_id}-s`, node: d.start, label: d.driver_id.replace(/^D0?/, 'D'), color: d.color, variant: 'truck', title: `${d.driver_id} starts at ${d.start_name}` },
    ...d.stops.map((s, i) => ({ id: `${d.driver_id}-${i}`, node: s.node, label: orderNo(s.order_id), color: s.kind === 'PICKUP' ? 'var(--restaurant)' : 'var(--society)', title: `${s.kind === 'PICKUP' ? 'Pick up' : 'Drop off'} ${s.order_id} at ${s.name}, ${minutes(s.arrival)}` })),
  ])
  const rejected = (report?.orders || []).filter(o => o.status !== 'DELIVERED')
  const stale = result && result.signature !== signature

  return <div className="page-stack">
    <PageHeading title="Plan a multi-stop dispatch"
      description="Queue orders and drivers, optionally close roads, and compare a greedy plan (each order appended to a driver) with cheapest insertion (orders woven into existing routes)."
      action={<div className="heading-buttons">
        <button type="button" className="button secondary" onClick={() => { setScenario(cleanScenario(SAMPLE)); setResult(null) }}><Sparkles size={16} /> Load sample</button>
        <button type="button" className="button secondary" onClick={share} disabled={!orders.length && !drivers.length}><Copy size={16} /> {copied ? 'Link copied' : 'Copy share link'}</button>
      </div>} />

    <div className="workbench planner">
      <div className="controls-col page-stack">
        <Panel className="panel-pad">
          <SectionTitle step={1} title="Orders" description="A restaurant pickup and a society drop-off. Times are simulator minutes from the start." />
          <div className="planner-fields">
            <SelectField id="plan-restaurant" label="Pickup restaurant" value={restaurant} onChange={setRestaurant} options={entityOptions(catalog.restaurants)} placeholder="Choose, or click the map" />
            <SelectField id="plan-society" label="Drop-off society" value={society} onChange={setSociety} options={entityOptions(catalog.societies)} placeholder="Choose, or click the map" />
            <div className="field"><label htmlFor="plan-ready">Food ready at <span className="optional">optional</span></label><input id="plan-ready" type="number" min="0" step="any" placeholder="min" value={ready} onChange={e => setReady(e.target.value)} /></div>
            <div className="field"><label htmlFor="plan-deadline">Deliver by <span className="optional">optional</span></label><input id="plan-deadline" type="number" min="0" step="any" placeholder="min" value={deadline} onChange={e => setDeadline(e.target.value)} /></div>
          </div>
          {ready !== '' && deadline !== '' && +deadline < +ready && <p className="field-error">The deadline must be at or after the ready time.</p>}
          {pair && <div className={`pair-check ${pair.serviceable ? 'ok' : 'blocked'}`} role="status">
            {pair.serviceable ? <CheckCircle2 size={17} /> : <ShieldAlert size={17} />}
            <div><strong>{pair.serviceable ? `Deliverable: ${kilometers(pair.distance_km)}, ${minutes(pair.time_min)}` : 'Blocked by the delivery policy'}</strong>
              <span>{pair.serviceable ? `${(pair.route?.length || 1) - 1} road hops, drawn on the map.` : `${pair.message}. The dashed red line shows the road it would take.`}</span>
              {blocked && suggestions.length > 0 && <div className="suggest"><small>Restaurants that can deliver here:</small>{suggestions.map(r => <button type="button" key={r.id} className="chip" onClick={() => setRestaurant(r.id)}>{r.name} <em>{kilometers(r.distance_km, 1)}</em></button>)}</div>}
            </div>
          </div>}
          <div className="add-row">
            <button className="button primary" type="button" disabled={!canAdd} onClick={addOrder}><Plus size={17} /> Add order</button>
            {!canAdd && addReason && <small className="muted">{addReason}</small>}
          </div>
          <div className="builder-list">
            <div className="list-header"><strong>Queue</strong><span>{orders.length} of {MAX_ORDERS}</span>{orders.length > 0 && <button type="button" className="text-button" onClick={() => update({ orders: [] })}>Clear</button>}</div>
            {orders.length ? <ol className="queue">{orders.map((item, i) => <li className="builder-item" key={i}>
              <span className="item-index" style={{ background: routeColor(i) }}>{i + 1}</span>
              <div><strong>{nameFor(item.restaurant, graph.nodes)} <ArrowRight size={13} /> {nameFor(item.society, graph.nodes)}</strong>
                <small>{item.blocked && <Badge kind="warning">Blocked by policy</Badge>} {item.km != null ? `${kilometers(item.km, 1)}, ` : ''}{item.ready_at != null ? `ready ${item.ready_at} min` : 'ready now'}{item.deadline != null ? `, by ${item.deadline} min` : ''}</small></div>
              <button type="button" className="icon-button remove" aria-label={`Remove order ${i + 1}`} onClick={() => update(s => ({ orders: s.orders.filter((_, j) => i !== j) }))}><Trash2 size={15} /></button>
            </li>)}</ol> : <p className="empty-list">No orders yet. Add one above, or load the sample.</p>}
          </div>
        </Panel>

        <Panel className="panel-pad">
          <SectionTitle step={2} title="Drivers" description="Each driver starts at a place on the graph." />
          <div className="inline-builder">
            <SelectField id="plan-driver" label="Starting place" value={driver} onChange={setDriver} options={entityOptions(graph.nodes).filter(option => !drivers.includes(option.value))} placeholder="Choose, or click the map" />
            <button className="button secondary" type="button" disabled={!driver || drivers.length >= MAX_DRIVERS} onClick={() => { update(s => ({ drivers: [...s.drivers, driver] })); setDriver('') }}><Plus size={17} /> Add</button>
          </div>
          <div className="driver-tags">{drivers.map((id, i) => <span className="driver-tag" key={id}><Truck size={14} /> D{i + 1}: {nameFor(id, graph.nodes)} <button type="button" aria-label={`Remove driver ${i + 1}`} onClick={() => update(s => ({ drivers: s.drivers.filter((_, j) => i !== j) }))}><X size={13} /></button></span>)}
            {!drivers.length && <p className="empty-list">No drivers yet.</p>}</div>
        </Panel>

        <Panel className="panel-pad">
          <SectionTitle step={3} title="Planning settings" />
          <div className="planner-fields">
            <div className="field"><label htmlFor="plan-capacity">Orders per driver (1–10)</label><input id="plan-capacity" type="number" min="1" max="10" step="1" value={settings.capacity} onChange={e => setSetting('capacity', e.target.value)} /></div>
            <SelectField id="plan-metric" label="Minimise" value={settings.metric} onChange={v => setSetting('metric', v)} options={[{ value: 'time', label: 'Travel time' }, { value: 'distance', label: 'Distance' }]} />
            <SelectField id="plan-algorithm" label="Shortest paths via" value={settings.algorithm} onChange={v => setSetting('algorithm', v)} options={[{ value: 'tree', label: 'Precomputed trees' }, { value: 'dijkstra', label: 'Dijkstra' }, { value: 'astar', label: 'A*' }]} />
            <SelectField id="plan-improve" label="Improvement pass" value={settings.improve} onChange={v => setSetting('improve', v)} options={[{ value: 'none', label: 'None' }, { value: 'intra', label: 'Reorder within a route' }, { value: 'relocate', label: 'Move orders between drivers' }]} />
            <div className="field"><label htmlFor="plan-ride">Max extra ride time <span className="optional">min, optional</span></label><input id="plan-ride" type="number" min="0" max="120" step="any" placeholder="No limit" value={settings.maxRide} onChange={e => setSetting('maxRide', e.target.value)} /></div>
          </div>
          {capacityInvalid && <p className="field-error">Orders per driver must be a whole number from 1 to 10.</p>}
          {rideInvalid && <p className="field-error">Extra ride time must be between 0 and 120 minutes.</p>}
        </Panel>

        <Panel className="panel-pad">
          <SectionTitle step={4} title="Road closures" description="Optional. Close one direction of a road at a given minute; drivers are rerouted." />
          <div className="planner-fields">
            <div className="field span-2"><SelectField id="plan-edge" label="Road (one direction)" value={edgeKey} onChange={setEdgeKey} options={edgeOptions} placeholder="Choose a road" /></div>
            <div className="field"><label htmlFor="plan-at">Close at minute</label><input id="plan-at" type="number" min="0" step="any" value={closureAt} onChange={e => setClosureAt(e.target.value)} /></div>
            <div className="field"><label htmlFor="plan-reopen">Reopen at <span className="optional">optional</span></label><input id="plan-reopen" type="number" min="0" step="any" placeholder="Stays closed" value={reopen} onChange={e => setReopen(e.target.value)} /></div>
          </div>
          {reopen !== '' && +reopen <= +closureAt && <p className="field-error">Reopen time must be after the closing time.</p>}
          <button type="button" className="button secondary" disabled={!edgeKey || closureInvalid || closures.length >= MAX_CLOSURES} onClick={addClosure}><Construction size={16} /> Add closure</button>
          {closures.length > 0 && <ul className="queue closures">{closures.map((item, i) => <li key={i} className="builder-item"><span className="item-index closure">{i + 1}</span>
            <div><strong>{nameFor(item.source, graph.nodes)} <ArrowRight size={13} /> {nameFor(item.target, graph.nodes)}</strong><small>closed from {item.at} min{item.reopen_at != null ? ` to ${item.reopen_at} min` : ''}</small></div>
            <button type="button" className="icon-button remove" aria-label={`Remove closure ${i + 1}`} onClick={() => update(s => ({ closures: s.closures.filter((_, j) => i !== j) }))}><Trash2 size={15} /></button></li>)}</ul>}
        </Panel>
      </div>

      <div className="map-col">
        <Panel className="map-panel sticky-map">
          <div className="map-panel-head">
            <Segmented label="Map click adds" value={pickMode} onChange={setPickMode} options={[{ value: 'pickup', label: 'Pickup', swatch: 'var(--restaurant)' }, { value: 'dropoff', label: 'Drop-off', swatch: 'var(--society)' }, { value: 'driver', label: 'Driver', swatch: 'var(--ink)' }]} />
          </div>
          <MapView graph={graph} className="fill" routes={composeRoutes} pins={composePins} closures={closures} onPick={onMapPick} pickable={pickable}
            fit={draftPath.length ? draftPath : null} highlight={[...orders.flatMap(o => [o.restaurant, o.society]), ...drivers]}
            legend={[{ type: 'line', color: 'var(--route)', label: 'Order being added' }, { type: 'line', color: '#8a969c', label: 'Queued orders' }, ...(closures.length ? [{ type: 'line', color: 'var(--danger)', dashed: true, label: 'Closed road' }] : [])]}
            emptyHint={pickMode === 'pickup' ? 'Click a restaurant (orange) to set the pickup' : pickMode === 'dropoff' ? 'Click a society (blue) to set the drop-off' : 'Click any place to add a driver there'} />
          <div className="run-box">
            <div className="scenario-counts"><span><strong>{orders.length}</strong> orders</span><span><strong>{drivers.length}</strong> drivers</span><span><strong>{closures.length}</strong> closures</span></div>
            <button type="button" className="button primary" disabled={!canRun || request.busy} onClick={run}><GitCompareArrows size={17} />{request.busy ? 'Comparing…' : 'Compare plans'}</button>
          </div>
          <BusyBar busy={request.busy} startedAt={request.startedAt} onCancel={request.cancel} label="Planning both methods" />
          <ErrorNote message={request.error} onDismiss={request.clearError} />
          {!canRun && <p className="muted small run-hint">{!orders.length ? 'Add at least one order.' : !drivers.length ? 'Add at least one driver.' : 'Fix the highlighted settings.'}</p>}
        </Panel>
      </div>
    </div>

    <section ref={resultsRef} tabIndex={-1} className="results-anchor page-stack" id="plan-results" aria-label="Plan comparison results">
      {!result ? <Callout icon={CircleHelp}>Nothing is precomputed. When you compare plans, both methods run on the server and every driver’s route is drawn here.</Callout> : <>
        <div className="result-head">
          <div><h2>Plan comparison</h2><p className="muted">{stale ? 'You changed the scenario since this run. Compare again to update.' : 'Both methods ran on the same orders, drivers and closures.'}</p></div>
          <div className={`gain ${result.distance_reduction > 0 ? 'up' : ''}`}>
            {result.distance_reduction == null ? <><strong>—</strong><span>{result.comparison_valid === false ? 'Methods delivered different orders, so distances are not comparable' : 'No delivered orders to compare'}</span></>
              : <><strong>{percent(result.distance_reduction)}</strong><span>less distance with insertion</span></>}
          </div>
        </div>

        <div className="two-col results-top">
          <Panel className="panel-pad">
            <SectionTitle title="Greedy vs insertion" description="Highlighted bars are the better value." />
            <div className="chart-key"><span><i className="swatch" style={{ background: '#9aa6ad' }} />{METHOD.append.name}</span><span><i className="swatch" style={{ background: 'var(--route)' }} />{METHOD.insertion.name}</span></div>
            <TotalsBars append={result.methods.append?.totals} insertion={result.methods.insertion?.totals} />
          </Panel>
          <Panel className="panel-pad">
            <SectionTitle title="Order outcomes" description={`${METHOD[method].name} plan.`} />
            <div className="outcome-row">{['delivered', 'late', 'rejected', 'failed'].map(k => <div key={k} className={`outcome ${k}`}><strong>{number(report?.totals?.[k])}</strong><span>{label(k)}</span></div>)}</div>
            {rejected.length > 0 ? <ul className="violations">{rejected.map(o => <li key={o.order_id}><AlertTriangle size={15} /> <span><strong>{o.order_id}</strong> {o.restaurant_name} to {o.society_name}: {label(o.status.toLowerCase())}{o.message ? `, ${o.message}` : o.reason ? `, ${label(String(o.reason).toLowerCase())}` : ''}</span></li>)}</ul>
              : <p className="muted">Every order was delivered.</p>}
            {(report?.violations || []).length > 0 && <Callout icon={AlertTriangle} tone="warn">Constraint check failed: {report.violations.join('; ')}</Callout>}
          </Panel>
        </div>

        <div className="route-layout plan-results-layout">
          <Panel className="map-panel">
            <div className="map-panel-head">
              <Segmented label="Plan shown" value={method} onChange={v => { setMethod(v); setFocus('') }} options={Object.entries(METHOD).map(([value, m]) => ({ value, label: m.name }))} />
              <span className="muted small">{number(report?.drivers?.length)} drivers, {kilometers(report?.totals?.distance_km, 1)}</span>
            </div>
            <MapView graph={graph} className="tall" routes={resultRoutes} pins={resultPins} closures={result.settings ? closures : []}
              fit={shownDrivers.flatMap(d => d.path)} fitKey={`${method}-${focus}-${result.signature.length}`}
              legend={[{ type: 'dot', color: 'var(--restaurant)', label: 'Pickup pin (order no.)' }, { type: 'dot', color: 'var(--society)', label: 'Drop-off pin (order no.)' }, { type: 'dot', color: 'var(--ink)', label: 'Square: driver start' }, { type: 'line', color: routeColor(0), label: 'One colour per driver' }]} />
          </Panel>
          <Panel className="panel-pad driver-panel">
            <SectionTitle title="Driver routes" description="Click a driver to isolate their route." />
            <div className="driver-cards">{driverRoutes.map(d => <button type="button" key={d.driver_id} className={`driver-card ${focus === d.driver_id ? 'active' : ''}`} onClick={() => setFocus(focus === d.driver_id ? '' : d.driver_id)} aria-pressed={focus === d.driver_id}>
              <span className="dc-head"><i className="swatch" style={{ background: d.color }} /><strong>{d.driver_id}</strong><span>{kilometers(d.distance_km, 1)}, {minutes(d.drive_time_min)}</span></span>
              <span className="dc-stops">{d.stops.map((s, i) => <span key={i} className={`stop-chip ${s.kind === 'PICKUP' ? 'p' : 'd'}`} title={`${s.kind === 'PICKUP' ? 'Pick up' : 'Drop off'} ${s.order_id} at ${s.name}, arrive ${minutes(s.arrival)}`}>{s.kind === 'PICKUP' ? 'Pick ' : 'Drop '}{orderNo(s.order_id)}</span>)}</span>
            </button>)}{!driverRoutes.length && <Empty icon={Truck} title="No driver moved" description="No order could be served in this plan." />}</div>
            {focus && (() => { const d = driverRoutes.find(x => x.driver_id === focus); return d ? <>
              <h3 className="small-title">Stops for {d.driver_id}</h3>
              <ol className="stop-list">{d.stops.map((s, i) => <li key={i}><span className={`stop-chip ${s.kind === 'PICKUP' ? 'p' : 'd'}`}>{s.kind === 'PICKUP' ? 'Pick ' : 'Drop '}{orderNo(s.order_id)}</span><div><strong>{s.name}</strong><small>arrive {minutes(s.arrival)}{s.departure > s.arrival + 0.01 ? `, wait until ${minutes(s.departure)}` : ''}, {kilometers(s.cum_km, 1)} so far</small></div></li>)}</ol>
              <RouteLegs legs={d.legs} nodes={graph.nodes} color={d.color} title="Full road path" maxRows={8} />
            </> : null })()}
          </Panel>
        </div>

        {(report?.reroutes || []).length > 0 && <Panel className="panel-pad">
          <SectionTitle title="Closures and reroutes" description="Closures are applied on dispatch ticks, so a road may reopen a little after the requested minute." />
          <ul className="plain-list">{report.reroutes.map((r, i) => <li key={i}><Badge kind={r.action === 'close' ? 'danger' : 'success'}>{r.action === 'close' ? 'Closed' : 'Reopened'}</Badge> {nameFor(r.road[0], graph.nodes)} → {nameFor(r.road[1], graph.nodes)} at {minutes(r.at)}; {r.affected_drivers.length ? `rerouted ${r.affected_drivers.join(', ')}` : 'no driver affected'} ({decimal(r.latency_ms, 1)} ms)</li>)}</ul>
        </Panel>}
        <Callout icon={Route}>Insertion is a heuristic, not a proof of the optimal plan. Distance savings are only shown when both methods delivered the same set of orders.</Callout>
      </>}
    </section>
  </div>
}

