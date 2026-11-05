import { GitCompareArrows, LayoutDashboard, Map, Menu, Route, Truck, X } from 'lucide-react'
import { useEffect, useState } from 'react'
import { NavLink, Route as BrowserRoute, Routes, useLocation } from 'react-router-dom'
import { useConnection, useHealthPoll, useResource } from './api'
import Overview from './pages/Overview'
import Coverage from './pages/Coverage'
import RouteLab from './pages/RouteLab'
import FleetLab from './pages/FleetLab'
import Planner from './pages/Planner'
import { State } from './components/Shared'

const navigation = [
  { to: '/', name: 'Overview', icon: LayoutDashboard, end: true, hint: 'The network at a glance' },
  { to: '/coverage', name: 'Coverage', icon: Map, hint: 'Who can deliver where' },
  { to: '/routes', name: 'Route lab', icon: Route, hint: 'Dijkstra vs A*' },
  { to: '/fleet', name: 'Fleet lab', icon: Truck, hint: 'Direct vs relay' },
  { to: '/planner', name: 'Planner', icon: GitCompareArrows, hint: 'Multi-stop plans' },
]

function BrandMark() {
  return <svg width="34" height="34" viewBox="0 0 34 34" aria-hidden="true">
    <rect width="34" height="34" rx="9" fill="#f3c623" />
    <path d="M9 25 C 9 17, 25 19, 25 10" fill="none" stroke="#16232b" strokeWidth="3.2" strokeLinecap="round" />
    <path d="M9 25 C 9 17, 25 19, 25 10" fill="none" stroke="#f3c623" strokeWidth="1" strokeDasharray="2.2 2.6" strokeLinecap="round" />
    <circle cx="9" cy="25" r="3.4" fill="#e1541f" stroke="#16232b" strokeWidth="1.4" />
    <circle cx="25" cy="10" r="3.4" fill="#3949c7" stroke="#16232b" strokeWidth="1.4" />
  </svg>
}

export default function App() {
  const catalog = useResource('/catalog')
  const graph = useResource('/graph')
  const connection = useConnection()
  const location = useLocation()
  const [menuOpen, setMenuOpen] = useState(false)
  useHealthPoll()
  useEffect(() => { setMenuOpen(false); window.scrollTo({ top: 0, behavior: 'instant' }) }, [location.pathname])
  const isReady = Boolean(catalog.data && graph.data)
  const online = connection.online !== false && !(catalog.error || graph.error)
  const current = navigation.find(item => item.to === location.pathname) || navigation[0]
  const pageProps = { catalog: catalog.data, graph: graph.data }
  return <div className="app-shell">
    <a href="#main" className="skip-link">Skip to content</a>
    {menuOpen && <button className="mobile-scrim" aria-label="Close navigation" onClick={() => setMenuOpen(false)} />}
    <aside className={`sidebar ${menuOpen ? 'open' : ''}`}>
      <div className="brand"><BrandMark /><div><strong>Route Studio</strong><small>Sangli, Miraj &amp; Kupwad</small></div>
        <button className="mobile-close icon-button" aria-label="Close menu" onClick={() => setMenuOpen(false)}><X size={20} /></button></div>
      <nav aria-label="Main navigation">{navigation.map(({ to, name, icon: Icon, end, hint }) => <NavLink key={to} to={to} end={end} className={({ isActive }) => `nav-item ${isActive ? 'active' : ''}`}>
        <Icon size={18} strokeWidth={1.9} aria-hidden="true" /><span><b>{name}</b><small>{hint}</small></span></NavLink>)}</nav>
      <div className="sidebar-bottom">
        <div className={`conn ${online ? (isReady ? 'ok' : 'wait') : 'down'}`}><i />{online ? (isReady ? 'Simulator connected' : 'Connecting to simulator') : 'Simulator offline'}</div>
        <p>A local simulation of food delivery on the Sangli road graph. Orders, drivers and ETAs are modelled, not live.</p>
      </div>
    </aside>
    <div className="main-shell">
      <div className="topbar">
        <button className="mobile-menu icon-button" aria-label="Open navigation" aria-expanded={menuOpen} onClick={() => setMenuOpen(true)}><Menu size={22} /></button>
        <span className="topbar-current">{current.name}</span>
        <span className={`conn compact ${online ? (isReady ? 'ok' : 'wait') : 'down'}`}><i />{online ? (isReady ? 'Connected' : 'Connecting') : 'Offline'}</span>
      </div>
      {!online && isReady && <div className="offline-banner" role="alert">The simulator API stopped responding. Start it again with <code>python run.py</code> in <code>backend/</code>; this page reconnects automatically.</div>}
      <main id="main" className="page-content">
        <State loading={catalog.loading || graph.loading} error={catalog.error || graph.error} retry={() => { catalog.retry(); graph.retry() }} label="the delivery network">
          {isReady && <Routes>
            <BrowserRoute path="/" element={<Overview {...pageProps} />} />
            <BrowserRoute path="/coverage" element={<Coverage {...pageProps} />} />
            <BrowserRoute path="/routes" element={<RouteLab {...pageProps} />} />
            <BrowserRoute path="/fleet" element={<FleetLab {...pageProps} />} />
            <BrowserRoute path="/planner" element={<Planner {...pageProps} />} />
            <BrowserRoute path="*" element={<Overview {...pageProps} />} />
          </Routes>}
        </State>
      </main>
    </div>
  </div>
}
