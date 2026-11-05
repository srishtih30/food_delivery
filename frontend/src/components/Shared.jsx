import { useEffect, useState } from 'react'
import { AlertCircle, ArrowRight, LoaderCircle, RotateCcw, Square, WifiOff } from 'lucide-react'
import { Link } from 'react-router-dom'
import { decimal, nameFor, number } from '../api'

export function Panel({ children, className = '', as: Tag = 'section', ...props }) {
  return <Tag className={`panel ${className}`} {...props}>{children}</Tag>
}

export function PageHeading({ title, description, action, kicker }) {
  return <header className="page-heading">
    <div>{kicker && <p className="kicker">{kicker}</p>}<h1>{title}</h1>{description && <p className="page-description">{description}</p>}</div>
    {action && <div className="heading-action">{action}</div>}
  </header>
}

export function SectionTitle({ title, description, action, level = 2, step }) {
  const H = `h${level}`
  return <div className="section-head"><div>
    <H>{step != null && <span className="step-num" aria-hidden="true">{step}</span>}{title}</H>
    {description && <p className="muted">{description}</p>}
  </div>{action && <div className="section-action">{action}</div>}</div>
}

export function Stat({ label, value, unit, note, icon: Icon, tone = 'teal', emphasis }) {
  return <div className={`stat ${emphasis ? 'emphasis' : ''}`}>
    <div className="stat-top"><span className="stat-label">{label}</span>{Icon && <span className={`stat-icon ${tone}`}><Icon size={16} aria-hidden="true" /></span>}</div>
    <div className="stat-value">{value ?? '—'}{unit && <span className="stat-unit">{unit}</span>}</div>
    {note && <p className="stat-note">{note}</p>}
  </div>
}

export function State({ loading, error, retry, children, label = 'simulator data' }) {
  if (!children && loading) return <Panel className="state-card" role="status"><LoaderCircle className="spin" size={22} aria-hidden="true" /><h2>Loading {label}</h2><p>Asking the local simulator.</p></Panel>
  if (!children && error) return <Panel className="state-card" role="alert"><AlertCircle size={22} aria-hidden="true" /><h2>Couldn’t load {label}</h2><p>{error}</p>{retry && <button className="button secondary" onClick={retry}><RotateCcw size={16} /> Try again</button>}</Panel>
  return <div className={loading ? 'refreshing' : ''} aria-busy={loading || undefined}>
    {error && <ErrorNote message={error} onRetry={retry} />}
    {children}
  </div>
}

export function ErrorNote({ message, onRetry, onDismiss }) {
  if (!message) return null
  const offline = /reach the simulator/i.test(message)
  return <div className={`inline-error ${offline ? 'offline' : ''}`} role="alert">
    {offline ? <WifiOff size={17} aria-hidden="true" /> : <AlertCircle size={17} aria-hidden="true" />}
    <span>{message}</span>
    {onRetry && <button type="button" onClick={onRetry}>Retry</button>}
    {onDismiss && <button type="button" onClick={onDismiss}>Dismiss</button>}
  </div>
}

/** Shows how long a request has been running, with a way to stop it. */
export function BusyBar({ busy, startedAt, onCancel, label = 'Computing' }) {
  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    if (!busy) return
    const timer = setInterval(() => setNow(Date.now()), 200)
    return () => clearInterval(timer)
  }, [busy])
  if (!busy) return null
  const seconds = Math.max(0, (now - startedAt) / 1000)
  return <div className="busy-bar" role="status" aria-live="polite">
    <span className="busy-track"><span /></span>
    <LoaderCircle className="spin" size={16} aria-hidden="true" />
    <span>{label}… {seconds.toFixed(1)} s</span>
    {onCancel && <button type="button" className="busy-cancel" onClick={onCancel}><Square size={12} aria-hidden="true" /> Cancel</button>}
  </div>
}

export function Empty({ title, description, icon: Icon, children }) {
  return <div className="empty">{Icon && <Icon size={26} strokeWidth={1.6} aria-hidden="true" />}<strong>{title}</strong>{description && <p>{description}</p>}{children}</div>
}

export function SelectField({ label, id, value, onChange, options, placeholder, disabled, hint }) {
  return <div className="field"><label htmlFor={id}>{label}</label>
    <select id={id} value={value} onChange={event => onChange(event.target.value)} disabled={disabled}>
      <option value="" disabled>{placeholder || 'Select an option'}</option>
      {options.map(option => <option key={option.value} value={option.value} disabled={option.disabled}>{option.label}</option>)}
    </select>{hint && <small>{hint}</small>}</div>
}

export const entityOptions = list => (list || []).map(item => ({ value: item.id, label: `${item.name} (${item.id})` }))

export function Badge({ children, kind = 'neutral' }) { return <span className={`badge ${kind}`}>{children}</span> }

export function Segmented({ value, onChange, options, label }) {
  return <div className="segmented" role="group" aria-label={label}>
    {options.map(option => <button type="button" key={option.value} aria-pressed={value === option.value} className={value === option.value ? 'active' : ''} onClick={() => onChange(option.value)}>
      {option.swatch && <i className="swatch" style={{ background: option.swatch }} />}{option.label}</button>)}
  </div>
}

export function Comparison({ rows, first = 'Dijkstra', second = 'A*', firstColor, secondColor }) {
  return <div className="table-scroll"><table className="compare-table"><thead><tr><th scope="col">Measure</th>
    <th scope="col">{firstColor && <i className="swatch" style={{ background: firstColor }} />}{first}</th>
    <th scope="col">{secondColor && <i className="swatch" style={{ background: secondColor }} />}{second}</th></tr></thead>
    <tbody>{rows.map(([label, a, b, better]) => <tr key={label}><th scope="row">{label}</th><td className={better === 1 ? 'better' : ''}>{a}</td><td className={better === 2 ? 'better' : ''}>{b}</td></tr>)}</tbody></table></div>
}

export function Callout({ children, icon: Icon = ArrowRight, tone = 'info' }) { return <div className={`callout ${tone}`}><Icon size={18} aria-hidden="true" /><div>{children}</div></div> }

export function LinkedAction({ to, children }) { return <Link to={to} className="text-link">{children} <ArrowRight size={15} aria-hidden="true" /></Link> }
export function Measurement({ value, unit, digits = 1 }) { return <>{value == null ? '—' : decimal(value, digits)}{value == null ? '' : ` ${unit}`}</> }
export function Count({ value }) { return number(value) }

/**
 * Route summary: totals plus a hop-by-hop strip whose bar widths are
 * proportional to distance.
 */
export function RouteLegs({ legs = [], nodes = [], color = 'var(--route)', maxRows = 14, title = 'Turn-by-turn' }) {
  if (!legs.length) return null
  const total = legs.reduce((s, l) => s + (l.km || 0), 0) || 1
  const time = legs.reduce((s, l) => s + (l.min || 0), 0)
  const shown = legs.length > maxRows ? [...legs.slice(0, maxRows - 2), null, legs.at(-1)] : legs
  return <div className="route-legs">
    <div className="legs-head"><strong>{title}</strong><span>{legs.length} {legs.length === 1 ? 'hop' : 'hops'}, {decimal(total, 2)} km, {decimal(time)} min</span></div>
    <div className="legs-strip" aria-hidden="true">{legs.map((leg, i) => <span key={i} style={{ flexGrow: Math.max(0.02, (leg.km || 0) / total), background: color, opacity: 0.55 + 0.45 * ((i % 2) ? 0.6 : 1) }} />)}</div>
    <ol className="legs-list"><li className="leg-start"><span className="leg-dot filled" style={{ background: color, borderColor: color }} /><span className="leg-name">{nameFor(legs[0].from, nodes)}</span><span className="leg-num">start</span><span className="leg-num" /></li>{shown.map((leg, i) => leg == null
      ? <li key="gap" className="legs-gap">… {legs.length - maxRows + 1} more hops</li>
      : <li key={i}><span className="leg-dot" style={{ borderColor: color }} /><span className="leg-name">{nameFor(leg.to, nodes)}</span><span className="leg-num">{decimal(leg.km, 2)} km</span><span className="leg-num">{decimal(leg.min)} min</span></li>)}
    </ol>
  </div>
}
