import React from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import App from './App'
import './styles.css'

class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props)
    this.state = { error: null }
  }
  static getDerivedStateFromError(error) {
    return { error }
  }
  componentDidCatch(error, info) {
    console.error('React Component Error:', error, info)
  }
  render() {
    if (this.state.error) {
      return (
        <div style={{ color: '#ca414d', padding: '24px', fontFamily: 'sans-serif' }}>
          <h2>Application Error</h2>
          <pre style={{ background: '#fff', padding: '16px', border: '1px solid #e4ebf3', borderRadius: '8px', overflow: 'auto' }}>
            {this.state.error?.stack || String(this.state.error)}
          </pre>
        </div>
      )
    }
    return this.props.children
  }
}

const rootEl = document.getElementById('root')
if (rootEl) {
  createRoot(rootEl).render(
    <React.StrictMode>
      <ErrorBoundary>
        <BrowserRouter>
          <App />
        </BrowserRouter>
      </ErrorBoundary>
    </React.StrictMode>
  )
}
