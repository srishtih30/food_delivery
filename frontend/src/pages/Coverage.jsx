import { useMemo } from 'react'
import { Check, CircleHelp, Link2, Search, ShieldCheck, ShieldX, Store } from 'lucide-react'
import { decimal, kilometers, minutes, query, useResource } from '../api'
import MapView from '../components/MapView'
import { Badge, Callout, Empty, PageHeading, Panel, RouteLegs, SectionTitle, Segmented, SelectField, State, entityOptions } from '../components/Shared'
import { isRestaurant } from '../lib/geo'
import { useUrlState } from '../lib/persist'

const REASON_TEXT = {
  OUTSIDE_SERVICE_ZONE: 'Zone rule',
  DELIVERY_DISTANCE_EXCEEDED: 'Too far',
  DELIVERY_TIME_EXCEEDED: 'Too slow',
  NO_ROAD_ROUTE: 'No road',
  RESTAURANT_NOT_ACCEPTING_ORDERS: 'Paused',
  INVALID_RESTAURANT_OR_SOCIETY: 'Invalid',
}

export default function Coverage({ catalog, graph }) {
  const societies = catalog.societies || []
  const [url, setUrl] = useUrlState({ society: '', restaurant: '', q: '', show: 'all' })
  const societyId = url.society, restaurantId = url.restaurant
  const coverage = useResource(societyId ? query('/coverage', { society: societyId }) : null)
  const check = useResource(societyId && restaurantId ? query('/serviceability', { restaurant: restaurantId, society: societyId }) : null)
  const data = coverage.data
  const restaurants = useMemo(() => data?.restaurants || [], [data])
  const byId = useMemo(() => Object.fromEntries(restaurants.map(r => [r.id, r])), [restaurants])
  const rows = restaurants
    .filter(item => url.show === 'all' || (url.show === 'yes' ? item.serviceable : !item.serviceable))
    .filter(item => `${item.name} ${item.cuisine || ''} ${item.area || ''} ${item.id}`.toLowerCase().includes(url.q.trim().toLowerCase()))
    .sort((a, b) => (b.serviceable - a.serviceable) || ((a.distance_km ?? 999) - (b.distance_km ?? 999)) || a.name.localeCompare(b.name))
  const selectedRestaurant = byId[restaurantId]
  const society = societies.find(item => item.id === societyId)

  const pair = check.data
  const route = pair?.route?.length ? pair.route : pair?.preview?.route || []
  const legs = pair?.route?.length ? pair.legs : pair?.preview?.legs || []
  const isPreview = Boolean(pair && !pair.route?.length && pair.preview)
  const distance = pair?.distance_km ?? pair?.preview?.distance_km
  const time = pair?.time_min ?? pair?.preview?.time_min

  const nodeStyle = useMemo(() => {
    if (!data) return null
    return node => {
      if (!isRestaurant(node)) return node.id === societyId ? { fill: 'var(--society)', r: 7 } : { opacity: 0.45 }
      const r = byId[node.id]
      if (!r) return null
      return r.serviceable ? { fill: 'var(--ok)', r: 3.6 } : { fill: '#9aa6ad', r: 2.6, opacity: 0.7 }
    }
  }, [data, byId, societyId])

  const linkedRoutes = useMemo(() => restaurants.filter(r => r.linked).map(r => ({ id: `l-${r.id}`, path: [r.id, societyId], color: 'var(--society)', ghost: true, width: 2.5 })), [restaurants, societyId])
  const routes = route.length ? [...linkedRoutes, { id: 'pair', path: route, color: isPreview ? 'var(--danger)' : 'var(--route)', dashed: isPreview, width: 5.5 }] : linkedRoutes
  const pins = [
    ...(societyId ? [{ node: societyId, label: 'S', color: 'var(--society)' }] : []),
    ...(restaurantId ? [{ node: restaurantId, label: 'R', color: 'var(--restaurant)' }] : []),
  ]
  const legend = societyId ? [
    { type: 'dot', color: 'var(--ok)', label: 'Can deliver' },
    { type: 'dot', color: '#9aa6ad', label: 'Not allowed' },
    { type: 'line', color: 'var(--society)', label: 'Listed direct link' },
    ...(route.length ? [{ type: 'line', color: isPreview ? 'var(--danger)' : 'var(--route)', dashed: isPreview, label: isPreview ? 'Road path (order blocked)' : 'Delivery route' }] : []),
  ] : [{ type: 'dot', color: 'var(--society)', label: 'Society, click to inspect' }, { type: 'dot', color: 'var(--restaurant)', label: 'Restaurant' }]

  function pickNode(node) {
    if (isRestaurant(node)) { if (societyId) setUrl({ restaurant: node.id }) }
    else setUrl({ society: node.id, restaurant: '', q: '' })
  }

  return <div className="page-stack">
    <PageHeading title="Who can deliver where"
      description="Choose a housing society. Every restaurant is checked against the zone policy and the distance and time limits, and the map shows the road the order would travel." />

    <div className="workbench">
      <Panel className="panel-pad controls-col">
        <SelectField label="Housing society" id="coverage-society" value={societyId} onChange={value => setUrl({ society: value, restaurant: '', q: '' })} options={entityOptions(societies)} placeholder="Choose a society, or click one on the map" />
        {society && <p className="muted small">{society.area || 'Area not listed'}, {society.zone || 'no'} zone</p>}
        {!societyId ? <Empty icon={Search} title="Start with a destination" description="Societies are the blue dots on the map. Pick one to colour every restaurant by whether it may deliver there." />
          : <State loading={coverage.loading} error={coverage.error} retry={coverage.retry} label="coverage">{data && <>
            <div className="mini-stats">
              <div><ShieldCheck size={16} /><strong>{data.counts?.serviceable}</strong><span>can deliver</span></div>
              <div><Link2 size={16} /><strong>{data.counts?.linked}</strong><span>direct links</span></div>
              <div><Store size={16} /><strong>{restaurants.length}</strong><span>checked</span></div>
            </div>
            <Segmented label="Show restaurants" value={url.show} onChange={value => setUrl({ show: value })} options={[{ value: 'all', label: 'All' }, { value: 'yes', label: 'Can deliver' }, { value: 'no', label: 'Blocked' }]} />
            <div className="search-field"><Search size={16} aria-hidden="true" /><input aria-label="Filter restaurants by name, cuisine, area or ID" value={url.q} onChange={e => setUrl({ q: e.target.value })} placeholder="Filter by name, cuisine or area" /></div>
            <div className="restaurant-list" role="list">{rows.map(item => <button type="button" role="listitem" key={item.id} className={`restaurant-row ${restaurantId === item.id ? 'selected' : ''}`} onClick={() => setUrl({ restaurant: item.id })} aria-pressed={restaurantId === item.id}>
              <span className={`status-dot ${item.serviceable ? 'ok' : 'no'}`} aria-hidden="true" />
              <span className="restaurant-copy"><strong>{item.name}</strong><small>{[item.area, item.zone].filter(Boolean).join(', ')}{item.linked ? ', direct link' : ''}</small></span>
              <span className="restaurant-meta">{item.serviceable ? kilometers(item.distance_km, 1) : <Badge kind="warning">{REASON_TEXT[item.reason] || 'Blocked'}</Badge>}</span>
            </button>)}{rows.length === 0 && <Empty title="No restaurants match" description="Clear the filter or switch the view above." />}</div>
          </>}</State>}
      </Panel>

      <div className="map-col">
        <Panel className="map-panel sticky-map">
          <MapView graph={graph} className="fill" nodeStyle={nodeStyle} routes={routes} pins={pins} selected={restaurantId || undefined}
            onPick={pickNode} pickable={societyId ? undefined : n => !isRestaurant(n)} legend={legend}
            fit={route.length ? route : societyId ? [societyId, ...restaurants.filter(r => r.serviceable).map(r => r.id)] : null}
            tooltipExtra={node => byId[node.id] ? <span className={byId[node.id].serviceable ? 'tip-ok' : 'tip-no'}>{byId[node.id].serviceable ? `Can deliver, ${decimal(byId[node.id].distance_km, 1)} km` : byId[node.id].message}</span> : null}
            emptyHint={societyId && !restaurantId ? 'Click a restaurant to draw its delivery route' : ''} />
        </Panel>
        <Panel className="panel-pad">
          <SectionTitle title={selectedRestaurant ? `${selectedRestaurant.name} to ${society?.name || societyId}` : 'Pair check'} description={selectedRestaurant ? 'Policy result and the road path for this pair.' : 'Choose a restaurant in the list or on the map.'} />
          {!selectedRestaurant ? <Empty icon={CircleHelp} title="No restaurant chosen" description="The route, distance and the exact policy rule appear here." />
            : <State loading={check.loading} error={check.error} retry={check.retry} label="pair check">{pair && <div className="pair-details">
              <div className="pair-verdict">
                <Badge kind={pair.serviceable ? 'success' : 'warning'}>{pair.serviceable ? <><Check size={13} /> Can deliver</> : <><ShieldX size={13} /> Blocked by policy</>}</Badge>
                <span>{pair.serviceable ? 'Within zone rules and limits.' : pair.message}</span>
              </div>
              <div className="detail-grid">
                <div><span>Restaurant zone</span><strong>{pair.restaurant_zone || '—'}</strong></div>
                <div><span>Society zone</span><strong>{pair.society_zone || '—'}</strong></div>
                <div><span>Road distance</span><strong>{kilometers(distance)}</strong></div>
                <div><span>Travel time</span><strong>{minutes(time)}</strong></div>
              </div>
              {isPreview && <Callout icon={ShieldX} tone="warn">The dashed red line is the fastest road path. The zone rule blocks the order before distance is considered, so this route is shown for reference only.</Callout>}
              <RouteLegs legs={legs} nodes={graph.nodes} color={isPreview ? 'var(--danger)' : 'var(--route)'} title={isPreview ? 'Road path (blocked order)' : 'Delivery route'} />
            </div>}</State>}
        </Panel>
      </div>
    </div>
    <Callout icon={CircleHelp}>A <strong>direct link</strong> is a listed road between a society and a restaurant. <strong>Can deliver</strong> is the policy decision. Neither implies the other: a restaurant two hops away can be allowed, and a directly linked one can be blocked by its zone.</Callout>
  </div>
}

