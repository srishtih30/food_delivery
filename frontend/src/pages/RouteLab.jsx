import { useEffect, useMemo, useRef, useState } from 'react'
import { ArrowRightLeft, ChevronLeft, ChevronRight, CircleHelp, Flag, MapPin, Navigation, Pause, Play, RotateCcw, Route, Search } from 'lucide-react'
import { decimal, kilometers, minutes, nameFor, number, query, useRequest } from '../api'
import MapView from '../components/MapView'
import { BusyBar, Callout, Comparison, Empty, ErrorNote, PageHeading, Panel, RouteLegs, SectionTitle, Segmented, SelectField, Stat, entityOptions } from '../components/Shared'
import { graphIndex, pathLegs } from '../lib/geo'
import { revealResults, useUrlState } from '../lib/persist'

const ALGO = {
  dijkstra: { name: 'Dijkstra', color: '#0f8f8a' },
  astar: { name: 'A*', color: '#5b4bd6' },
}
const METRIC = 'distance'   // routes are always the shortest by distance

/** Rebuild what the search knew at a given step: visited set and parents. */
function traceState(steps, upTo) {
  const visited = new Set()
  const parent = {}
  for (let i = 0; i <= upTo && i < steps.length; i++) {
    const s = steps[i]
    if (s.kind === 'expand' || s.kind === 'goal') visited.add(s.node)
    for (const r of s.relaxed || []) parent[r.node] = s.node
  }
  return { visited, parent }
}

function pathTo(parent, node, source) {
  const path = [node]
  const seen = new Set(path)
  while (path[0] !== source && parent[path[0]] && !seen.has(parent[path[0]])) {
    path.unshift(parent[path[0]])
    seen.add(path[0])
  }
  return path[0] === source ? path : []
}

/** One algorithm's map, replayed up to the shared frame. */
function SearchSide({ id, algo, onAlgo, result, frame, graph, index, source, target, onPick, pickHint, unit }) {
  const search = result?.[algo], steps = search?.steps || []
  const step = Math.min(frame, Math.max(0, steps.length - 1))
  const trace = steps[step]
  const complete = !steps.length || step >= steps.length - 1
  const { visited, parent } = useMemo(() => traceState(steps, step), [steps, step])
  const frontier = useMemo(() => (trace?.frontier_top || []).map(item => item.node), [trace])
  const tentative = useMemo(() => (!complete && trace?.node && result) ? pathTo(parent, trace.node, result.source) : [], [complete, trace, parent, result])
  const legs = useMemo(() => search?.legs?.length ? search.legs : pathLegs(index, search?.path), [search, index])
  const color = ALGO[algo].color

  const routes = []
  if (result) {
    if (complete) routes.push({ id: algo, path: search?.path || [], color, width: 5.5 })
    else if (tentative.length > 1) routes.push({ id: 'tentative', path: tentative, color: '#c2891a', width: 4.5, dashed: true })
  }
  const pins = [
    ...(source ? [{ node: source, label: 'A', color: 'var(--ink)' }] : []),
    ...(target ? [{ node: target, label: 'B', color }] : []),
  ]
  const legend = result ? [
    { type: 'line', color, label: `${ALGO[algo].name} route` },
    ...(!complete ? [{ type: 'line', color: '#c2891a', dashed: true, label: 'Best path to current node' }] : []),
    { type: 'ring', color: '#7aa0b8', label: 'Explored' },
    { type: 'ring', color: '#c2891a', label: 'Frontier' },
  ] : [{ type: 'dot', color: 'var(--restaurant)', label: 'Restaurant' }, { type: 'dot', color: 'var(--society)', label: 'Society' }]

  return <Panel className="map-panel">
    <div className="dual-head">
      <i className="swatch" style={{ background: color }} />
      <div className="field"><label htmlFor={`algo-${id}`} className="sr-only">Algorithm shown on this map</label>
        <select id={`algo-${id}`} value={algo} onChange={e => onAlgo(e.target.value)} aria-label={`Algorithm for the ${id} map`}>
          {Object.entries(ALGO).map(([value, a]) => <option key={value} value={value}>{a.name}</option>)}
        </select></div>
      {result && <span className="muted small">{number(search?.nodes_visited)} nodes explored</span>}
    </div>
    <MapView graph={graph} className="tall" routes={routes} pins={pins} explored={result ? [...visited] : undefined} frontier={result ? frontier : undefined}
      selected={trace && !complete ? trace.node : undefined} highlight={[source, target]} legend={legend}
      onPick={onPick} fit={result ? (search?.path?.length ? search.path : [result.source, result.target]) : (source && target ? [source, target] : null)}
      emptyHint={!result ? pickHint : ''} label={`${ALGO[algo].name} search map`} />
    {result && <div className="dual-trace">
      <div className="trace-state"><span className={`trace-kind ${trace?.kind}`}>{complete ? 'Route found' : trace?.kind === 'stale' ? 'Skipped outdated entry' : 'Expanding node'}</span>
        <span className="dual-done">step {step + 1} of {steps.length}</span></div>
      <div className="current-node"><small>Current node</small><h3>{trace?.node ? nameFor(trace.node, graph.nodes) : '—'}</h3></div>
      <div className="trace-costs">
        <div><span>g, travelled</span><strong>{decimal(trace?.g, 2)}</strong></div>
        <div><span>h, estimate</span><strong>{decimal(trace?.h, 2)}</strong></div>
        <div><span>f = g + h</span><strong>{decimal(trace?.f, 2)}</strong></div>
      </div>
      {complete && <RouteLegs legs={legs} nodes={graph.nodes} color={color} title={`${ALGO[algo].name} route`} maxRows={7} />}
    </div>}
  </Panel>
}

export default function RouteLab({ graph }) {
  const [url, setUrl] = useUrlState({ from: '', to: '', a: 'dijkstra', b: 'astar' })
  const [source, setSource] = useState(url.from), [target, setTarget] = useState(url.to)
  const left = ALGO[url.a] ? url.a : 'dijkstra', right = ALGO[url.b] ? url.b : 'astar'
  const [result, setResult] = useState(null)
  const [frame, setFrame] = useState(0), [playing, setPlaying] = useState(false), [speed, setSpeed] = useState(220)
  const [pickSlot, setPickSlot] = useState(url.from ? 'to' : 'from')
  const request = useRequest({ timeout: 20000 })
  const resultsRef = useRef(null)
  const index = useMemo(() => graphIndex(graph), [graph])

  // Both maps share one replay clock; the shorter search simply finishes first.
  const lastFrame = Math.max(0, (result?.[left]?.steps?.length || 1) - 1, (result?.[right]?.steps?.length || 1) - 1)
  const done = frame >= lastFrame

  useEffect(() => {
    if (!playing) return
    const timer = setInterval(() => setFrame(value => {
      if (value >= lastFrame) { setPlaying(false); return value }
      return value + 1
    }), speed)
    return () => clearInterval(timer)
  }, [playing, speed, lastFrame])

  async function run(event, opts = {}) {
    event?.preventDefault()
    if (!source || !target || source === target) return
    setPlaying(false)
    setUrl({ from: source, to: target })
    const data = await request.run(query('/search', { source, target, metric: METRIC }))
    if (!data) return
    setResult(data)
    // Show the finished routes first; replay starts from the beginning on Play.
    setFrame(Math.max(0, (data.dijkstra?.steps?.length || 1) - 1, (data.astar?.steps?.length || 1) - 1))
    if (!opts.silent) revealResults(resultsRef)
  }

  // Restore a shared or reloaded link.
  useEffect(() => { if (url.from && url.to && url.from !== url.to) run(null, { silent: true }) }, []) // eslint-disable-line react-hooks/exhaustive-deps

  function pick(node) {
    if (pickSlot === 'from') { setSource(node.id); setPickSlot('to') }
    else { setTarget(node.id); setPickSlot('from') }
  }
  const pickHint = pickSlot === 'from' ? 'Click a place to set the start (A)' : 'Click a place to set the destination (B)'

  const unit = result?.unit || 'km'
  const d = result?.dijkstra, a = result?.astar
  const savedPct = d?.nodes_visited ? 1 - a.nodes_visited / d.nodes_visited : null
  const sideProps = { result, frame, graph, index, source, target, onPick: pick, pickHint, unit }

  return <div className="page-stack">
    <PageHeading title="Dijkstra and A*, side by side"
      description="Pick two places. Both searches run on the server and are replayed together, so you can see how much of the city each one explores before it finds the shortest route by distance." />

    <Panel className="panel-pad">
      <form onSubmit={run} className="route-controls">
        <SelectField id="route-source" label="Start (A)" value={source} onChange={setSource} options={entityOptions(graph.nodes)} placeholder="Choose, or click a map" />
        <button type="button" className="swap-button icon-button" aria-label="Swap start and destination" onClick={() => { setSource(target); setTarget(source) }}><ArrowRightLeft size={18} /></button>
        <SelectField id="route-target" label="Destination (B)" value={target} onChange={setTarget} options={entityOptions(graph.nodes)} placeholder="Choose, or click a map" />
        <button type="submit" className="button primary" disabled={!source || !target || source === target || request.busy}><Search size={17} />{request.busy ? 'Searching…' : 'Find route'}</button>
      </form>
      {source && source === target && <p className="field-error">Start and destination must be different places.</p>}
      <BusyBar busy={request.busy} startedAt={request.startedAt} onCancel={request.cancel} label="Searching" />
      <ErrorNote message={request.error} onDismiss={request.clearError} />
    </Panel>

    <section ref={resultsRef} tabIndex={-1} className="results-anchor page-stack" aria-label="Route search results">
      {result && <div className="stats-row four">
        <Stat label="Dijkstra route" value={decimal(d?.cost, 2)} unit={unit} note={`${number(d?.nodes_visited)} nodes explored`} icon={Route} emphasis />
        <Stat label="A* route" value={decimal(a?.cost, 2)} unit={unit} note={`${number(a?.nodes_visited)} nodes explored`} icon={Route} tone="violet" />
        <Stat label="Travel time" value={decimal(d?.total_time_min)} unit="min" note="Simulator road times" icon={Flag} tone="amber" />
        <Stat label="Search saved" value={savedPct != null ? `${decimal(Math.abs(savedPct) * 100, 0)}%` : '—'} note={savedPct != null ? `A* explored ${savedPct >= 0 ? 'fewer' : 'more'} nodes than Dijkstra` : ''} icon={Search} tone="chili" />
      </div>}

      <div className="map-panel-head panel pick-bar">
        <Segmented label="Map click sets" value={pickSlot} onChange={setPickSlot} options={[{ value: 'from', label: 'Click sets A' }, { value: 'to', label: 'Click sets B' }]} />
        <span className="muted small">Each map has its own algorithm menu. Both maps stay in step.</span>
      </div>

      <div className="dual-maps">
        <SearchSide id="left" algo={left} onAlgo={value => setUrl({ a: value })} {...sideProps} />
        <SearchSide id="right" algo={right} onAlgo={value => setUrl({ b: value })} {...sideProps} />
      </div>

      {result ? <Panel className="dual-playback">
        <div className="playback">
          <div className="playback-buttons">
            <button className="icon-button" aria-label="Restart replay" onClick={() => { setFrame(0); setPlaying(false) }}><RotateCcw size={17} /></button>
            <button className="icon-button" aria-label="Previous step" disabled={frame === 0} onClick={() => { setFrame(Math.max(0, frame - 1)); setPlaying(false) }}><ChevronLeft size={20} /></button>
            <button className="button primary play-button" disabled={lastFrame < 1} aria-label={playing ? 'Pause replay' : 'Play replay'} onClick={() => { if (done) setFrame(0); setPlaying(!playing) }}>{playing ? <Pause size={17} /> : <Play size={17} />}</button>
            <button className="icon-button" aria-label="Next step" disabled={done} onClick={() => { setFrame(Math.min(lastFrame, frame + 1)); setPlaying(false) }}><ChevronRight size={20} /></button>
          </div>
          <div className="playback-range"><label htmlFor="route-step">Step {frame + 1} of {lastFrame + 1}{done ? ', both routes found' : ''}</label>
            <input id="route-step" type="range" min="0" max={lastFrame} value={frame} onChange={e => { setFrame(+e.target.value); setPlaying(false) }} /></div>
          <select value={speed} aria-label="Replay speed" onChange={e => setSpeed(+e.target.value)}><option value="500">Slow</option><option value="220">Normal</option><option value="80">Fast</option></select>
        </div>
      </Panel> : <Panel className="panel-pad"><Empty icon={MapPin} title="No search yet" description="Choose A and B above or on either map, then find the route. You can replay both searches afterwards." /></Panel>}

      {result && <Panel>
        <div className="panel-pad"><SectionTitle title="Same problem, two searches" description="Both must find a route of equal cost; A* uses a distance estimate to explore less of the graph." /></div>
        <Comparison firstColor={ALGO.dijkstra.color} secondColor={ALGO.astar.color} rows={[
          ['Route cost', `${decimal(d?.cost, 2)} ${unit}`, `${decimal(a?.cost, 2)} ${unit}`],
          ['Distance', kilometers(d?.total_distance_km), kilometers(a?.total_distance_km)],
          ['Travel time', minutes(d?.total_time_min), minutes(a?.total_time_min)],
          ['Nodes explored', number(d?.nodes_visited), number(a?.nodes_visited), a?.nodes_visited < d?.nodes_visited ? 2 : a?.nodes_visited > d?.nodes_visited ? 1 : 0],
          ['Edges relaxed', number(d?.edges_relaxed), number(a?.edges_relaxed), a?.edges_relaxed < d?.edges_relaxed ? 2 : 0],
          ['Run time', `${decimal(d?.execution_time_ms, 3)} ms`, `${decimal(a?.execution_time_ms, 3)} ms`],
          ['Trace check', d?.verified ? 'Matches untraced run' : 'Not verified', a?.verified ? 'Matches untraced run' : 'Not verified'],
        ]} />
      </Panel>}
      {result && <Callout icon={CircleHelp}>Routes are the shortest by distance. Road weights are directional: the route from A to B can differ from B to A. Cost is the optimisation objective, not a delivery promise.</Callout>}
    </section>
  </div>
}
