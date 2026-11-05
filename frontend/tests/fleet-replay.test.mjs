import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { measure, pointAt } from '../src/lib/geo.js'
const source = readFileSync(new URL('../src/pages/FleetLab.jsx', import.meta.url), 'utf8')
const body = source.slice(source.indexOf('function positionAt('), source.indexOf('/** Driver markers'))
const positionAt = vm.runInNewContext(body + '; positionAt', { pointAt })
const order = { a: 'D1', b: 'D2', pickup: 2, h: 'H', ht: 5, delivered: 12 }
const track = { id: 'D1', orders: [order], start: [0, 0], segs: [
  { t0: 2, t1: 4, m: measure([[0,0],[10,0]]) },
  { t0: 9, t1: 11, m: measure([[10,0],[0,0]]) }
] }
test('sender waits at meeting, releases food, then follows only its next job', () => {
  assert.equal(positionAt(track, 5).p[0], 10)
  assert.equal(positionAt(track, 5).carrying, true)
  assert.equal(positionAt(track, 7).carrying, false)
  assert.equal(positionAt(track, 8).p[0], 10)
  assert.equal(positionAt(track, 10).p[0], 5)
})
test('receiver takes custody only when transfer completes; seeks are deterministic', () => {
  const receiver = { ...track, id: 'D2' }
  assert.equal(positionAt(receiver, 6).carrying, false)
  assert.equal(positionAt(receiver, 7).carrying, true)
  assert.equal(positionAt(receiver, 12).carrying, false)
  assert.equal(positionAt(receiver, 3).p[0], 5)
})
