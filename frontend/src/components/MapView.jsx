import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { Crosshair, Layers, Minus, Plus, Search, X } from 'lucide-react'
import { bounds, graphIndex, isRestaurant, nodePoint, pathLine, toPoints } from '../lib/geo'
import { prefersReducedMotion, readStored, writeStored } from '../lib/persist'

const PICK_RADIUS_PX = 18
const HOVER_RADIUS_PX = 14
const MIN_VIEW_W = 18

const RESTAURANT = 'var(--restaurant)'
const SOCIETY = 'var(--society)'

/* ---------------------------------------------------------------- */
/* Static layers (memoised: they only change with the graph / zoom)  */
/* ---------------------------------------------------------------- */
const RoadLayer = memo(function RoadLayer({ index, tiles }) {
  const lines = useMemo(() => [...index.edges.values()].map(edge => index.hop(edge.a, edge.b)).filter(l => l.length > 1), [index])
  return <g className={`road-layer ${tiles ? 'on-tiles' : ''}`} aria-hidden="true">
    {lines.map((line, i) => <polyline key={i} points={toPoints(line)} vectorEffect="non-scaling-stroke" />)}
  </g>
})

const NodeLayer = memo(function NodeLayer({ index, k, emphasis, explored, frontier, nodeStyle, selected }) {
  return <g className="node-layer" aria-hidden="true">
    {index.nodes.map(node => {
      const p = nodePoint(index, node.id)
      if (!p) return null
      const restaurant = isRestaurant(node)
      const custom = nodeStyle?.(node) || {}
      const strong = emphasis.has(node.id) || selected === node.id
      const r = (custom.r ?? (strong ? 5.4 : restaurant ? 3.1 : 4.2)) * k
      const fill = custom.fill || (restaurant ? RESTAURANT : SOCIETY)
      const ex = explored?.has(node.id), fr = frontier?.has(node.id)
      return <g key={node.id} opacity={custom.opacity ?? 1}>
        {(ex || fr) && <circle cx={p[0]} cy={p[1]} r={9 * k} className={fr ? 'trace-frontier' : 'trace-explored'} />}
        {selected === node.id && <circle cx={p[0]} cy={p[1]} r={11 * k} className="node-selected-ring" />}
        <circle cx={p[0]} cy={p[1]} r={r} fill={fill} stroke="#fff" strokeWidth={(restaurant && !strong ? 1.2 : 1.8) * k} />
      </g>
    })}
  </g>
})

function Pin({ x, y, k, label, color = 'var(--ink)', variant = 'drop', title }) {
  return <g transform={`translate(${x} ${y}) scale(${k})`} className="map-pin">
    {title && <title>{title}</title>}
    {variant === 'ring'
      ? <><circle r="9" fill="#fff" /><circle r="6.5" fill={color} /><circle r="2.4" fill="#fff" /></>
      : variant === 'truck'
        ? <><rect x="-11" y="-11" width="22" height="22" rx="6" fill={color} stroke="#fff" strokeWidth="2" /><text y="4" textAnchor="middle" className="pin-text">{label}</text></>
        : <><path d="M0 0 C-2 -6 -11 -10 -11 -19 A11 11 0 1 1 11 -19 C11 -10 2 -6 0 0Z" fill={color} stroke="#fff" strokeWidth="2" /><text y="-15.5" textAnchor="middle" className="pin-text">{label}</text></>}
  </g>
}

function Tiles({ projection, view, k, onStatus }) {
  const unit = projection?.unit
  if (!unit) return null
  const dpr = typeof window !== 'undefined' ? window.devicePixelRatio || 1 : 1
  const z = Math.max(3, Math.min(19, Math.round(Math.log2(unit / k / 256 * (dpr > 1.4 ? 1 : 1)))))
  const n = 2 ** z
  const X0 = projection.origin_x + view.x / unit, X1 = projection.origin_x + (view.x + view.w) / unit
  const Y0 = projection.origin_y + view.y / unit, Y1 = projection.origin_y + (view.y + view.h) / unit
  const tx0 = Math.floor(X0 * n), tx1 = Math.floor(X1 * n), ty0 = Math.floor(Y0 * n), ty1 = Math.floor(Y1 * n)
  if ((tx1 - tx0 + 1) * (ty1 - ty0 + 1) > 80) return null
  const size = unit / n, overlap = 0.6 * k
  const retina = dpr > 1.4 ? '@2x' : ''
  const tiles = []
  for (let tx = tx0; tx <= tx1; tx++) for (let ty = ty0; ty <= ty1; ty++) {
    tiles.push(<image key={`${z}/${tx}/${ty}`} href={`https://basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}.png?key=cb1_506h_1_a542ff3e5c51c41096f412f1`}
      x={(tx / n - projection.origin_x) * unit} y={(ty / n - projection.origin_y) * unit} width={size + overlap} height={size + overlap}
      preserveAspectRatio="none" onLoad={() => onStatus('load')} onError={() => onStatus('error')} />)
  }
  return <g className="tile-layer" aria-hidden="true">{tiles}</g>
}

/**
 * The shared map. Routes are node paths (drawn along street geometry) or
 * raw point lists; pins mark stops; overlay(k) draws animated layers.
 */
export default function MapView({
  graph, routes = [], pins = [], highlight = [], explored, frontier, closures = [], selected,
  nodeStyle, onPick, pickable, fit, fitKey, overlay, legend = [], label = 'Delivery network map',
  className = '', toolbarExtra, tooltipExtra, emptyHint,
}) {
  const index = useMemo(() => graphIndex(graph), [graph])
  const wrapRef = useRef(null), svgRef = useRef(null)
  const [size, setSize] = useState({ w: 800, h: 520 })
  const [view, setView] = useState(null)
  const viewRef = useRef(null)
  const [hovered, setHovered] = useState(null)
  const [tilesOn, setTilesOn] = useState(() => readStored('map.tiles', true))
  const [tileState, setTileState] = useState({ loaded: 0, failed: 0 })
  const [hint, setHint] = useState('')
  const [findText, setFindText] = useState('')
  const [findOpen, setFindOpen] = useState(false)
  const anim = useRef(0)

  const k = view ? view.w / Math.max(1, size.w) : 1
  viewRef.current = view

  // -- sizing ---------------------------------------------------------
  useLayoutEffect(() => {
    const el = wrapRef.current
    if (!el) return
    const update = () => {
      const rect = el.getBoundingClientRect()
      setSize(s => (Math.abs(s.w - rect.width) < 1 && Math.abs(s.h - rect.height) < 1) ? s : { w: Math.max(50, rect.width), h: Math.max(50, rect.height) })
    }
    update()
    const observer = new ResizeObserver(update)
    observer.observe(el)
    return () => observer.disconnect()
  }, [])

  // keep viewBox aspect equal to the element aspect
  useEffect(() => {
    setView(v => v ? { ...v, h: v.w * size.h / size.w } : v)
  }, [size.w, size.h])

  const fitView = useCallback((points, animate = true, minW = 70) => {
    const b = bounds(points)
    if (!b) return
    const aspect = size.h / size.w
    // Usable pixel box inside the overlays: search box on top, legend below.
    const padT = 70, padB = 58, padX = 40
    const availW = Math.max(80, size.w - padX * 2), availH = Math.max(80, size.h - padT - padB)
    const w = Math.max(minW, ((b.maxX - b.minX) || 1) * size.w / availW, ((b.maxY - b.minY) || 1) * size.w / availH)
    const h = w * aspect
    const unitsPerPx = w / size.w
    const cx = (b.minX + b.maxX) / 2, cy = (b.minY + b.maxY) / 2 - ((padT - padB) / 2) * unitsPerPx
    const target = { x: cx - w / 2, y: cy - h / 2, w, h }
    const from = viewRef.current
    cancelAnimationFrame(anim.current)
    if (!animate || !from || prefersReducedMotion()) { setView(target); return }
    const start = performance.now(), duration = 420
    const step = now => {
      const t = Math.min(1, (now - start) / duration), e = 1 - Math.pow(1 - t, 3)
      setView({ x: from.x + (target.x - from.x) * e, y: from.y + (target.y - from.y) * e, w: from.w + (target.w - from.w) * e, h: (from.w + (target.w - from.w) * e) * aspect })
      if (t < 1) anim.current = requestAnimationFrame(step)
    }
    anim.current = requestAnimationFrame(step)
  }, [size.w, size.h])

  const allPoints = useMemo(() => index ? index.nodes.map(n => nodePoint(index, n.id)) : [], [index])
  const fitPoints = useMemo(() => {
    if (!index || !fit || !fit.length) return null
    return fit.flatMap(item => typeof item === 'string' ? [nodePoint(index, item)] : Array.isArray(item?.[0]) ? item : [item]).filter(Boolean)
  }, [index, fit])
  const autoKey = fitKey ?? (fit ? JSON.stringify(fit).slice(0, 400) : '')

  // initial fit + refit when the requested focus changes
  const sized = size.w > 60
  useEffect(() => {
    if (!index || !sized) return
    if (fitPoints?.length) fitView(fitPoints, Boolean(viewRef.current), 60)
    else if (!viewRef.current) fitView(allPoints, false)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [index, sized, autoKey])

  // -- interaction ----------------------------------------------------
  const toSvg = useCallback((clientX, clientY) => {
    const rect = svgRef.current.getBoundingClientRect()
    const v = viewRef.current
    return [v.x + (clientX - rect.left) * (v.w / rect.width), v.y + (clientY - rect.top) * (v.h / rect.height)]
  }, [])

  const zoomAt = useCallback((point, factor) => {
    setView(v => {
      if (!v) return v
      const maxW = (index?.width || 1000) * 2.4
      const w = Math.max(MIN_VIEW_W, Math.min(maxW, v.w * factor))
      const real = w / v.w
      return { x: point[0] - (point[0] - v.x) * real, y: point[1] - (point[1] - v.y) * real, w, h: w * size.h / size.w }
    })
  }, [index, size.w, size.h])

  useEffect(() => {
    const svg = svgRef.current
    if (!svg) return
    const onWheel = event => {
      if (!viewRef.current) return
      event.preventDefault()
      cancelAnimationFrame(anim.current)
      const delta = event.deltaMode === 1 ? event.deltaY * 16 : event.deltaY
      zoomAt(toSvg(event.clientX, event.clientY), Math.exp(delta * 0.0016))
    }
    svg.addEventListener('wheel', onWheel, { passive: false })
    return () => svg.removeEventListener('wheel', onWheel)
  }, [zoomAt, toSvg])

  const candidatesNear = useCallback((point, radiusPx, filter) => {
    const kk = viewRef.current.w / Math.max(1, size.w)
    const found = []
    for (const node of index.nodes) {
      if (filter && !filter(node)) continue
      const p = nodePoint(index, node.id)
      if (!p) continue
      const d = Math.hypot(p[0] - point[0], p[1] - point[1]) / kk
      if (d <= radiusPx) found.push({ node, d })
    }
    return found.sort((a, b) => a.d - b.d)
  }, [index, size.w])

  const pointers = useRef(new Map())
  const gesture = useRef(null)

  const onPointerDown = event => {
    if (!viewRef.current) return
    svgRef.current.setPointerCapture?.(event.pointerId)
    pointers.current.set(event.pointerId, [event.clientX, event.clientY])
    cancelAnimationFrame(anim.current)
    const list = [...pointers.current.values()]
    if (list.length === 1) gesture.current = { kind: 'pan', start: list[0], last: list[0], moved: 0 }
    else if (list.length === 2) {
      const [a, b] = list
      gesture.current = { kind: 'pinch', dist: Math.hypot(a[0] - b[0], a[1] - b[1]), moved: 99 }
    }
  }

  const hoverFrame = useRef(0)
  const onPointerMove = event => {
    if (!viewRef.current) return
    if (!pointers.current.has(event.pointerId)) {
      if (event.pointerType !== 'mouse') return
      const { clientX, clientY } = event
      cancelAnimationFrame(hoverFrame.current)
      hoverFrame.current = requestAnimationFrame(() => {
        const near = candidatesNear(toSvg(clientX, clientY), HOVER_RADIUS_PX, onPick ? pickable : null)[0]
        setHovered(near ? near.node.id : null)
      })
      return
    }
    pointers.current.set(event.pointerId, [event.clientX, event.clientY])
    const g = gesture.current
    if (!g) return
    const rect = svgRef.current.getBoundingClientRect()
    const scale = viewRef.current.w / rect.width
    if (g.kind === 'pan') {
      const dx = event.clientX - g.last[0], dy = event.clientY - g.last[1]
      g.moved += Math.abs(dx) + Math.abs(dy)
      g.last = [event.clientX, event.clientY]
      if (g.moved > 4) setView(v => ({ ...v, x: v.x - dx * scale, y: v.y - dy * scale }))
    } else if (g.kind === 'pinch') {
      const [a, b] = [...pointers.current.values()]
      const dist = Math.hypot(a[0] - b[0], a[1] - b[1])
      if (g.dist > 0 && dist > 0) zoomAt(toSvg((a[0] + b[0]) / 2, (a[1] + b[1]) / 2), g.dist / dist)
      g.dist = dist
    }
  }

  const pick = useCallback((point, viaTouch) => {
    if (!onPick) return
    const radius = viaTouch ? PICK_RADIUS_PX + 8 : PICK_RADIUS_PX
    const near = candidatesNear(point, radius, pickable)
    if (!near.length) return
    const v = viewRef.current
    // Several places stacked under the pointer: zoom into the cluster first
    // instead of guessing which one was meant.
    if (near.length > 1 && near[1].d - near[0].d < 5 && near[1].d < 12 && v.w > MIN_VIEW_W * 2.2) {
      zoomAt(point, 0.35)
      setHint(`${near.length} places overlap here. Zoomed in, tap again to choose.`)
      return
    }
    setHint('')
    onPick(near[0].node)
  }, [onPick, pickable, candidatesNear, zoomAt])

  const onPointerUp = event => {
    const g = gesture.current
    pointers.current.delete(event.pointerId)
    if (pointers.current.size === 0) {
      if (g && g.kind === 'pan' && g.moved <= 6) pick(toSvg(event.clientX, event.clientY), event.pointerType !== 'mouse')
      gesture.current = null
    } else if (pointers.current.size === 1) {
      const rest = [...pointers.current.values()][0]
      gesture.current = { kind: 'pan', start: rest, last: rest, moved: 99 }
    }
  }

  useEffect(() => { if (!hint) return; const t = setTimeout(() => setHint(''), 3200); return () => clearTimeout(t) }, [hint])

  // -- derived drawing data -----------------------------------------
  const emphasis = useMemo(() => new Set(highlight.filter(Boolean)), [highlight])
  const exploredSet = useMemo(() => explored ? new Set(explored) : null, [explored])
  const frontierSet = useMemo(() => frontier ? new Set(frontier) : null, [frontier])
  const drawnRoutes = useMemo(() => routes.map((route, i) => ({
    ...route, key: route.id ?? i,
    line: route.points || pathLine(index, route.path || []),
  })).filter(route => route.line.length > 1), [routes, index])
  const closureLines = useMemo(() => closures.map(c => ({ ...c, line: index?.hop(c.source, c.target) || [] })).filter(c => c.line.length > 1), [closures, index])

  const onTileStatus = useCallback(kind => setTileState(s => kind === 'load' ? (s.loaded > 2 ? s : { ...s, loaded: s.loaded + 1 }) : { ...s, failed: s.failed + 1 }), [])
  const tilesOffline = tilesOn && tileState.failed >= 3 && tileState.loaded === 0

  const findResults = useMemo(() => {
    const q = findText.trim().toLowerCase()
    if (!q || !index) return []
    return index.nodes.filter(node => (!onPick || !pickable || pickable(node)) && `${node.name} ${node.id} ${node.area || ''}`.toLowerCase().includes(q)).slice(0, 7)
  }, [findText, index, onPick, pickable])

  function choose(node) {
    const p = nodePoint(index, node.id)
    if (p) fitView([p], true, 90)
    setFindText(''); setFindOpen(false)
    if (onPick && (!pickable || pickable(node))) onPick(node)
    else setHovered(node.id)
  }

  if (!index || !index.nodes.length) return <div className={`map-stage ${className}`}><p className="map-empty">The simulator has not returned any map data yet.</p></div>

  const hoveredNode = hovered ? index.byId.get(hovered) : null
  const hoveredPoint = hoveredNode && view ? nodePoint(index, hovered) : null

  return <div className={`map-stage ${className}`} ref={wrapRef}>
    <svg ref={svgRef} className={`map-svg ${onPick ? 'pickable' : ''}`} role="img" aria-label={`${label}. Use the place finder for keyboard selection.`}
      viewBox={view ? `${view.x} ${view.y} ${view.w} ${view.h}` : `0 0 ${index.width} ${index.height}`} preserveAspectRatio="none"
      onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={onPointerUp} onPointerCancel={onPointerUp}
      onPointerLeave={event => { if (event.pointerType === 'mouse' && !pointers.current.size) setHovered(null) }}
      onDoubleClick={event => zoomAt(toSvg(event.clientX, event.clientY), 0.5)}>
      {view && <rect x={view.x - view.w} y={view.y - view.h} width={view.w * 3} height={view.h * 3} className="map-bg" />}
      {tilesOn && view && <Tiles projection={graph.projection} view={view} k={k} onStatus={onTileStatus} />}
      <RoadLayer index={index} tiles={tilesOn && !tilesOffline} />
      {closureLines.map((c, i) => <g key={`c${i}`} className="closure-line"><polyline points={toPoints(c.line)} vectorEffect="non-scaling-stroke" className="closure-casing" /><polyline points={toPoints(c.line)} vectorEffect="non-scaling-stroke" className="closure-stroke" /></g>)}
      {drawnRoutes.filter(r => r.ghost).map(route => <polyline key={`g${route.key}`} points={toPoints(route.line)} className="route-ghost" stroke={route.color} vectorEffect="non-scaling-stroke" style={{ strokeWidth: route.width || 4 }} />)}
      {drawnRoutes.filter(r => !r.ghost).map(route => <g key={`r${route.key}`} className={`route ${route.dashed ? 'dashed' : ''}`} opacity={route.opacity ?? 1}>
        <polyline points={toPoints(route.line)} className="route-casing" vectorEffect="non-scaling-stroke" style={{ strokeWidth: (route.width || 5) + 4 }} />
        <polyline points={toPoints(route.line)} className="route-line" stroke={route.color || 'var(--route)'} vectorEffect="non-scaling-stroke" style={{ strokeWidth: route.width || 5 }} />
        {route.animate !== false && !route.dashed && <polyline points={toPoints(route.line)} className="route-flow" vectorEffect="non-scaling-stroke" style={{ strokeWidth: Math.max(1.5, (route.width || 5) - 3) }} />}
        {route.title && <title>{route.title}</title>}
      </g>)}
      <NodeLayer index={index} k={k} emphasis={emphasis} explored={exploredSet} frontier={frontierSet} nodeStyle={nodeStyle} selected={selected} />
      {pins.map((pin, i) => {
        const p = pin.point || nodePoint(index, pin.node)
        return p ? <Pin key={pin.id ?? `${pin.node}-${i}`} x={p[0]} y={p[1]} k={k} label={pin.label} color={pin.color} variant={pin.variant} title={pin.title} /> : null
      })}
      {overlay?.(k)}
    </svg>

    {hoveredNode && hoveredPoint && <div className="map-tooltip" style={{ left: (hoveredPoint[0] - view.x) / k, top: (hoveredPoint[1] - view.y) / k }}>
      <span className={`tip-kind ${isRestaurant(hoveredNode) ? 'r' : 's'}`}>{isRestaurant(hoveredNode) ? 'Restaurant' : 'Society'}</span>
      <strong>{hoveredNode.name}</strong>
      <small>{hoveredNode.id}{hoveredNode.area ? `, ${hoveredNode.area}` : ''}</small>
      {tooltipExtra?.(hoveredNode)}
      {onPick && (!pickable || pickable(hoveredNode)) && <em>Click to choose</em>}
    </div>}

    <div className="map-find">
      <Search size={15} aria-hidden="true" />
      <input value={findText} onChange={e => { setFindText(e.target.value); setFindOpen(true) }} onFocus={() => setFindOpen(true)}
        onKeyDown={e => { if (e.key === 'Enter' && findResults[0]) { e.preventDefault(); choose(findResults[0]) } if (e.key === 'Escape') setFindOpen(false) }}
        placeholder={onPick ? 'Find a place to choose' : 'Find a place'} aria-label="Find a place on the map" />
      {findText && <button type="button" className="find-clear" aria-label="Clear search" onClick={() => setFindText('')}><X size={14} /></button>}
      {findOpen && findResults.length > 0 && <ul className="find-results" role="listbox">
        {findResults.map(node => <li key={node.id}><button type="button" onMouseDown={e => e.preventDefault()} onClick={() => choose(node)}>
          <i className={isRestaurant(node) ? 'dot r' : 'dot s'} /><span>{node.name}</span><small>{node.id}</small></button></li>)}
      </ul>}
    </div>

    <div className="map-tools">
      {toolbarExtra}
      <button type="button" className="map-tool" aria-label="Zoom in" onClick={() => view && zoomAt([view.x + view.w / 2, view.y + view.h / 2], 0.6)}><Plus size={17} /></button>
      <button type="button" className="map-tool" aria-label="Zoom out" onClick={() => view && zoomAt([view.x + view.w / 2, view.y + view.h / 2], 1.6)}><Minus size={17} /></button>
      <button type="button" className="map-tool" aria-label={fitPoints?.length ? 'Fit to route' : 'Fit whole network'} title={fitPoints?.length ? 'Fit to route' : 'Fit whole network'} onClick={() => fitView(fitPoints?.length ? fitPoints : allPoints, true, 60)}><Crosshair size={16} /></button>
      <button type="button" className={`map-tool ${tilesOn ? 'active' : ''}`} aria-pressed={tilesOn} aria-label="Street basemap" title={tilesOn ? 'Hide street basemap' : 'Show street basemap'} onClick={() => { setTilesOn(v => { writeStored('map.tiles', !v); return !v }) }}><Layers size={16} /></button>
    </div>

    {hint && <div className="map-hint" role="status">{hint}</div>}
    {emptyHint && !hint && <div className="map-hint subtle">{emptyHint}</div>}

    {legend.length > 0 && <div className="map-legend">{legend.map((item, i) => <span key={i}>
      {item.type === 'line' ? <i className={`lg-line ${item.dashed ? 'dashed' : ''}`} style={{ background: item.dashed ? undefined : item.color, color: item.color }} /> : item.type === 'ring' ? <i className="lg-ring" style={{ borderColor: item.color }} /> : <i className="lg-dot" style={{ background: item.color }} />}
      {item.label}</span>)}</div>}
    <div className="map-attrib">{tilesOn && !tilesOffline ? <>© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">OpenStreetMap</a> © <a href="https://carto.com/attributions" target="_blank" rel="noreferrer">CARTO</a></> : tilesOffline ? 'Offline: simulator roads only' : 'Simulator road geometry'}</div>
  </div>
}

export const defaultLegend = [
  { type: 'dot', color: 'var(--restaurant)', label: 'Restaurant' },
  { type: 'dot', color: 'var(--society)', label: 'Society' },
]
