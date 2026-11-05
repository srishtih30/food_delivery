/* Geometry helpers over the /api/graph payload (projected SVG coordinates). */

const cacheByGraph = new WeakMap()

function pt(p) {
  if (Array.isArray(p) && Number.isFinite(+p[0]) && Number.isFinite(+p[1])) return [+p[0], +p[1]]
  if (p && Number.isFinite(+p.x) && Number.isFinite(+p.y)) return [+p.x, +p.y]
  return null
}

/** Index of nodes, undirected edges and cached hop polylines. */
export function graphIndex(graph) {
  if (!graph) return null
  if (cacheByGraph.has(graph)) return cacheByGraph.get(graph)
  const nodes = graph.nodes || []
  const byId = new Map(nodes.map(node => [node.id, node]))
  const edges = new Map()
  for (const edge of graph.edges || []) edges.set(edge.a < edge.b ? `${edge.a}|${edge.b}` : `${edge.b}|${edge.a}`, edge)
  const hopCache = new Map()

  /** Street polyline for the directed hop a -> b (oriented a to b). */
  function hop(a, b) {
    const key = `${a}>${b}`
    if (hopCache.has(key)) return hopCache.get(key)
    const start = pt(byId.get(a)), end = pt(byId.get(b))
    let line = []
    if (start && end) {
      const edge = edges.get(a < b ? `${a}|${b}` : `${b}|${a}`)
      const shape = (edge?.shape || []).map(pt).filter(Boolean)
      if (shape.length >= 2) {
        const first = shape[0], last = shape.at(-1)
        const forward = Math.hypot(first[0] - start[0], first[1] - start[1]) + Math.hypot(last[0] - end[0], last[1] - end[1])
        const backward = Math.hypot(last[0] - start[0], last[1] - start[1]) + Math.hypot(first[0] - end[0], first[1] - end[1])
        line = [start, ...(forward <= backward ? shape : [...shape].reverse()), end]
      } else line = [start, end]
    }
    hopCache.set(key, line)
    return line
  }

  /** Directed weights [km, min] for a -> b, or null. */
  function weights(a, b) {
    const edge = edges.get(a < b ? `${a}|${b}` : `${b}|${a}`)
    if (!edge) return null
    return (edge.a === a ? edge.ab : edge.ba) || null
  }

  function edgeLine(a, b) { return hop(a, b) }

  const index = { graph, nodes, byId, edges, hop, weights, edgeLine, width: +graph.width || 1000, height: +graph.height || 680 }
  cacheByGraph.set(graph, index)
  return index
}

/** One polyline along a node path. */
export function pathLine(index, path) {
  const out = []
  if (!index || !Array.isArray(path)) return out
  if (path.length === 1) { const p = pt(index.byId.get(path[0])); return p ? [p] : [] }
  for (let i = 0; i < path.length - 1; i++) {
    if (path[i] === path[i + 1]) continue
    const line = index.hop(path[i], path[i + 1])
    if (!line.length) continue
    if (out.length) out.push(...line.slice(1))
    else out.push(...line)
  }
  return out
}

/** Hop list with directional distance/time. */
export function pathLegs(index, path) {
  const legs = []
  if (!index || !Array.isArray(path)) return legs
  for (let i = 0; i < path.length - 1; i++) {
    const w = index.weights(path[i], path[i + 1])
    legs.push({ from: path[i], to: path[i + 1], km: w ? w[0] : null, min: w ? w[1] : null })
  }
  return legs
}

export function pathTotals(index, path) {
  return pathLegs(index, path).reduce((acc, leg) => ({ km: acc.km + (leg.km || 0), min: acc.min + (leg.min || 0) }), { km: 0, min: 0 })
}

/** Cumulative lengths for fast interpolation. */
export function measure(line) {
  const cum = [0]
  for (let i = 1; i < line.length; i++) cum.push(cum[i - 1] + Math.hypot(line[i][0] - line[i - 1][0], line[i][1] - line[i - 1][1]))
  return { line, cum, total: cum.at(-1) || 0 }
}

/** Point at fraction (0..1) of a measured line, by binary search. */
export function pointAt(measured, fraction) {
  const { line, cum, total } = measured
  if (!line.length) return null
  if (line.length === 1 || total === 0) return line[0]
  const target = Math.max(0, Math.min(1, fraction)) * total
  let lo = 0, hi = cum.length - 1
  while (lo < hi - 1) { const mid = (lo + hi) >> 1; if (cum[mid] <= target) lo = mid; else hi = mid }
  const span = cum[hi] - cum[lo] || 1
  const t = (target - cum[lo]) / span
  return [line[lo][0] + (line[hi][0] - line[lo][0]) * t, line[lo][1] + (line[hi][1] - line[lo][1]) * t]
}

export function bounds(points) {
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity
  for (const p of points) {
    if (!p) continue
    if (p[0] < minX) minX = p[0]
    if (p[1] < minY) minY = p[1]
    if (p[0] > maxX) maxX = p[0]
    if (p[1] > maxY) maxY = p[1]
  }
  return Number.isFinite(minX) ? { minX, minY, maxX, maxY } : null
}

export const toPoints = line => line.map(([x, y]) => `${x.toFixed(2)},${y.toFixed(2)}`).join(' ')
export const nodePoint = (index, id) => pt(index?.byId.get(id))
export const isRestaurant = node => node?.kind === 'Restaurant' || /^R\d/.test(node?.id || '')

/** Distinct, colour-blind-aware route colours (one per driver/route). */
export const ROUTE_COLORS = ['#0f8f8a', '#d9480f', '#5b4bd6', '#c2891a', '#c2367a', '#2f7ec9', '#4d8a2a', '#8a5a2b', '#0e6b9c', '#b23a3a']
export const routeColor = i => ROUTE_COLORS[((i % ROUTE_COLORS.length) + ROUTE_COLORS.length) % ROUTE_COLORS.length]
