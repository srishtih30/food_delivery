import { encodeShare } from './persist'

const SETTINGS = { capacity: 3, algorithm: 'tree', metric: 'time', improve: 'relocate', maxRide: '20' }
const orders = pairs => pairs.map(([restaurant, society]) => ({ restaurant, society }))

/** Planner test cases shown on the home page. Every pair is deliverable under the default policy. */
export const PLANNER_SAMPLES = [
  {
    id: 'one-to-many', title: 'One restaurant, many stops', shape: ['1 pickup', '4 drop-offs'],
    text: 'Four customers order from the same restaurant. One driver batches them into a single trip.',
    scenario: { orders: orders([['R179', 'S003'], ['R179', 'S004'], ['R179', 'S010'], ['R179', 'S005']]), drivers: ['R182'], closures: [], settings: { ...SETTINGS, capacity: 4 } },
  },
  {
    id: 'many-to-one', title: 'Many restaurants, one stop', shape: ['4 pickups', '1 drop-off'],
    text: 'One society orders from four restaurants. The driver collects everything, then delivers once.',
    scenario: { orders: orders([['R179', 'S003'], ['R183', 'S003'], ['R182', 'S003'], ['R172', 'S003']]), drivers: ['S010'], closures: [], settings: { ...SETTINGS, capacity: 4 } },
  },
  {
    id: 'one-to-one', title: 'One restaurant, one stop', shape: ['1 pickup', '1 drop-off'],
    text: 'The simplest case: a single order and a single driver, a long ride across town.',
    scenario: { orders: orders([['R203', 'S002']]), drivers: ['R100'], closures: [], settings: SETTINGS },
  },
]

/** `/planner` link that loads the scenario and runs the comparison. */
export const plannerLink = sample => `/planner?s=${encodeShare(sample.scenario)}&go=1`
