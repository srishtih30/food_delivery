import { useMemo, useState } from 'react'
import { ArrowRight, Building2, FlaskConical, GitCompareArrows, Map as MapIcon, Route, Store, Truck, X } from 'lucide-react'
import { Link } from 'react-router-dom'
import { decimal, nameFor, number } from '../api'
import MapView, { defaultLegend } from '../components/MapView'
import { Badge, PageHeading, Segmented } from '../components/Shared'
import { graphIndex, isRestaurant, routeColor } from '../lib/geo'
import { PLANNER_SAMPLES, plannerLink } from '../lib/samples'

const ZONE_COLORS = { Sangli: '#0f8f8a', Miraj: '#c2891a', Kupwad: '#5b4bd6' }

const workspaces = [
  { to: '/coverage', icon: MapIcon, name: 'Coverage', text: 'Pick a society and see which restaurants may deliver there, and the road each order would take.' },
  { to: '/routes', icon: Route, name: 'Route lab', text: 'Replay Dijkstra and A* side by side and compare how much of the city each one explores.' },
  { to: '/fleet', icon: Truck, name: 'Fleet lab', text: 'Watch drivers on the same road hand food to each other, and compare relay with direct delivery.' },
  { to: '/planner', icon: GitCompareArrows, name: 'Planner', text: 'Build your own orders and drivers, close roads, and compare greedy and insertion plans.' },
]

export default function Overview({ catalog, graph }) {
  const { restaurants = [], societies = [], links = [], summary = {} } = catalog
  const [colorBy, setColorBy] = useState('type')
  const [focus, setFocus] = useState('')
  const index = useMemo(() => graphIndex(graph), [graph])
  const zoneOf = useMemo(() => Object.fromEntries([...restaurants, ...societies].map(p => [p.id, p.zone])), [restaurants, societies])
  const zones = Object.keys(ZONE_COLORS)
  const place = focus ? [...restaurants, ...societies].find(p => p.id === focus) : null
  const node = focus ? index.byId.get(focus) : null

  // Direct road links of the focused place, drawn along street geometry.
  const focusLinks = useMemo(() => {
    if (!focus) return []
    return links.filter(l => l.society_id === focus || l.restaurant_id === focus)
      .map(l => ({ ...l, other: l.society_id === focus ? l.restaurant_id : l.society_id }))
      .sort((a, b) => (a.forward_km ?? 99) - (b.forward_km ?? 99))
  }, [focus, links])
  const routes = focusLinks.map((l, i) => ({ id: l.other, path: [focus, l.other], color: routeColor(i), width: 3.5, animate: false, title: `${nameFor(l.other, graph.nodes)}: ${decimal(l.forward_km, 2)} km` }))
  const nodeStyle = useMemo(() => colorBy === 'zone' ? (n => ({ fill: ZONE_COLORS[zoneOf[n.id]] || '#8a969c' })) : null, [colorBy, zoneOf])
  const legend = colorBy === 'zone' ? zones.map(z => ({ type: 'dot', color: ZONE_COLORS[z], label: z })) : defaultLegend

  return <div className="page-stack">
    <PageHeading title="Food delivery on the Sangli road network"
      description="248 restaurants and 20 housing societies across Sangli, Miraj and Kupwad, joined by 688 directed road links traced from OpenStreetMap. Every page below runs real searches and simulations on this graph." />

    <section className="hero-map panel" aria-label="Network map">
      <MapView graph={graph} className="tall" legend={legend} nodeStyle={nodeStyle} highlight={focus ? [focus, ...focusLinks.map(l => l.other)] : []}
        routes={routes} pins={node ? [{ node: focus, label: isRestaurant(node) ? 'R' : 'S', color: isRestaurant(node) ? 'var(--restaurant)' : 'var(--society)' }] : []}
        fit={focus ? [focus, ...focusLinks.map(l => l.other)] : null} onPick={n => setFocus(n.id)}
        emptyHint={focus ? '' : 'Click any place to see its direct road links'}
        toolbarExtra={<Segmented label="Colour places by" value={colorBy} onChange={setColorBy} options={[{ value: 'type', label: 'Type' }, { value: 'zone', label: 'Zone' }]} />} />
      {place && <aside className="place-card" aria-live="polite">
        <button type="button" className="icon-button close" aria-label="Close place details" onClick={() => setFocus('')}><X size={16} /></button>
        <span className={`tip-kind ${isRestaurant(node) ? 'r' : 's'}`}>{isRestaurant(node) ? 'Restaurant' : 'Housing society'}</span>
        <h2>{place.name}</h2>
        <p className="muted">{place.area || 'Area not listed'}, {place.zone || 'no zone'} zone{place.cuisine ? `. ${place.cuisine}` : ''}</p>
        <div className="place-links">
          <strong>{focusLinks.length} direct road {focusLinks.length === 1 ? 'link' : 'links'}</strong>
          <ol>{focusLinks.slice(0, 8).map((l, i) => <li key={l.other}><i className="swatch" style={{ background: routeColor(i) }} /><span>{nameFor(l.other, graph.nodes)}</span><small>{decimal(isRestaurant(node) ? l.return_km : l.forward_km, 2)} km</small></li>)}</ol>
          {focusLinks.length > 8 && <small className="muted">and {focusLinks.length - 8} more on the map</small>}
        </div>
        <div className="place-actions">
          {isRestaurant(node)
            ? <Link className="button secondary small" to={`/routes?from=${focus}`}>Route from here <ArrowRight size={14} /></Link>
            : <Link className="button secondary small" to={`/coverage?society=${focus}`}>Check coverage <ArrowRight size={14} /></Link>}
        </div>
      </aside>}
    </section>

    <section className="panel panel-pad" aria-labelledby="samples-title">
      <h2 id="samples-title" className="section-title-inline"><FlaskConical size={18} aria-hidden="true" /> Try a sample scenario</h2>
      <p className="muted">Each button opens the Planner already filled in and runs the comparison, so you can see how multi-stop dispatch handles a different shape of demand.</p>
      <div className="sample-grid">{PLANNER_SAMPLES.map(sample => <Link key={sample.id} to={plannerLink(sample)} className="sample-button">
        <strong>{sample.title}<ArrowRight size={15} aria-hidden="true" /></strong>
        <span className="sample-shape">{sample.shape.join(' → ')}</span>
        <small>{sample.text}</small>
      </Link>)}</div>
    </section>

    <div className="figure-row">
      <div><Store size={18} aria-hidden="true" /><strong>{number(summary.restaurants ?? restaurants.length)}</strong><span>restaurants</span></div>
      <div><Building2 size={18} aria-hidden="true" /><strong>{number(summary.societies ?? societies.length)}</strong><span>housing societies</span></div>
      <div><Route size={18} aria-hidden="true" /><strong>{number(summary.roads ?? graph.stats?.roads)}</strong><span>directed road links</span></div>
      <div><GitCompareArrows size={18} aria-hidden="true" /><strong>{number(summary.relationships ?? links.length)}</strong><span>listed restaurant–society pairs</span></div>
    </div>

    <div className="overview-grid">
      <section className="panel panel-pad">
        <h2>Service zones</h2>
        <p className="muted">Zones come from each address’s postal town. The policy lets every zone serve itself, plus Sangli and Kupwad serve each other. Distance is capped at {catalog.policy?.max_distance_km} km and {catalog.policy?.max_time_min} min.</p>
        <div className="zone-bars">{zones.map(zone => {
          const r = restaurants.filter(p => p.zone === zone).length, s = societies.filter(p => p.zone === zone).length
          return <div key={zone} className="zone-bar">
            <div className="zone-name"><i className="swatch" style={{ background: ZONE_COLORS[zone] }} /><strong>{zone}</strong><span>{r} restaurants, {s} {s === 1 ? 'society' : 'societies'}</span></div>
            <div className="bar"><span style={{ width: `${(r / restaurants.length) * 100}%`, background: ZONE_COLORS[zone] }} /></div>
          </div>
        })}</div>
        <div className="zone-pairs">{(catalog.policy?.allowed_zone_pairs || []).map(([a, b]) => <Badge key={`${a}-${b}`} kind="info">{a} to {b}</Badge>)}<Badge kind="neutral">Same zone</Badge></div>
      </section>
      <section className="workspace-list" aria-label="Workspaces">
        {workspaces.map(({ to, icon: Icon, name, text }) => <Link key={to} to={to} className="workspace-link">
          <span className="ws-icon"><Icon size={19} aria-hidden="true" /></span>
          <span><strong>{name}</strong><small>{text}</small></span>
          <ArrowRight size={17} className="ws-arrow" aria-hidden="true" />
        </Link>)}
      </section>
    </div>
  </div>
}
