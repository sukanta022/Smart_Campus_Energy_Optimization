// GridWise /optimize-energy load test.
//
// Target: 1k RPS for 10 minutes against the production endpoint.
// SLO:    p99 latency < 2.5 s, error rate < 0.1%, throughput >= 1000 RPS.
//
// Run with k6:
//   k6 run \
//     --out json=results.json \
//     --summary-trend-stats="avg,med,p(90),p(95),p(99),max" \
//     -e BASE_URL=https://api.gridwise.example.com \
//     -e SCENARIO_POOL_FILE=./scenarios.json \
//     tests/load/k6_optimize.js
//
// In CI, k6 produces machine-readable summary that the deploy workflow's
// post-deploy gate can fail on if SLOs regress.

import http from 'k6/http';
import { check, sleep } from 'k6';
import { Trend, Counter, Rate } from 'k6/metrics';
import { SharedArray } from 'k6/data';
import { randomIntBetween, randomItem } from 'https://jslib.k6.io/k6-utils/1.4.0/index.js';
import { uuidv4 } from 'https://jslib.k6.io/k6-utils/1.4.0/index.js';

// ---------- Custom metrics ----------

const optimizeLatency = new Trend('optimize_latency_ms', true);
const llmPathLatency  = new Trend('llm_path_latency_ms', true);
const fallbackLatency = new Trend('fallback_path_latency_ms', true);
const serviceErrors   = new Counter('service_errors_total'); // 5xx only
const clientErrors    = new Counter('client_errors_total'); // 4xx
const replayFailures  = new Counter('replay_failures_total');
const llmInUseRate    = new Rate('llm_in_use_rate');

// ---------- Configuration ----------

export const options = {
  scenarios: {
    peak_load: {
      executor: 'constant-arrival-rate',
      // 1000 RPS = 1000 iterations per 1000 ms; allow 50 in flight at any moment.
      rate: 1000,
      timeUnit: '1s',
      duration: '10m',
      preAllocatedVUs: 200,
      maxVUs: 2000,
      gracefulStop: '30s',
    },
  },
  thresholds: {
    // Hard SLO gates — k6 exits non-zero when any of these are violated.
    'optimize_latency_ms': ['p(99)<2500'],
    'http_req_failed':     ['rate<0.001'],   // < 0.1% errors overall
    'service_errors_total':['count<60'],     // < 60 5xx over 10 min @ 1k RPS = 0.01%
    'http_reqs':           ['count>=600000'],// 1k RPS * 600 s = 600k requests
    'iteration_duration':  ['p(99)<3000'],   // end-to-end including retry
    // Soft signal — we don't fail the run on this but it surfaces in the report.
    'replay_failures_total':['count<1'],
  },
  noConnectionReuse: false,
  userAgent: 'gridwise-k6/1.0',
};

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8080';

// ---------- Scenario pool ----------
//
// We load a realistic pool of energy scenarios to exercise:
//   - peak vs off-peak tariffs
//   - varied solar generation curves
//   - battery at different SoC starting points
//   - capacity mismatch (request > installed → must be validator-clamped)
//   - distractor directives (e.g. "test if solar > 50% of demand")
//
// When SCENARIO_POOL_FILE is set (e.g. via -e), we read it. Otherwise we fall
// back to a small in-script generator that produces the same shape PRD §07
// demands.

const poolFromFile = new SharedArray('scenarios', function () {
  if (!__ENV.SCENARIO_POOL_FILE) {
    return [];
  }
  const raw = open(__ENV.SCENARIO_POOL_FILE);
  return JSON.parse(raw);
});

function makeRandomScenario(scenarioId) {
  const hours = [];
  // Solar curve: bell shape peaking around hour 13, scaled by a random factor.
  const solarPeak = randomIntBetween(2, 8);
  for (let h = 0; h < 24; h++) {
    const bell = Math.exp(-((h - 13) ** 2) / 30);
    const solar = Math.round(solarPeak * bell * 100) / 100;
    // Demand: low overnight, two peaks at 09 and 19.
    const morning = Math.exp(-((h - 9) ** 2) / 12);
    const evening = Math.exp(-((h - 19) ** 2) / 8);
    const base = 1.5 + 2.5 * morning + 3.0 * evening;
    const demand = Math.round(base * 100) / 100;
    hours.push({ hour: h, solar_kwh: solar, demand_kwh: demand });
  }

  const directiveFamilies = [
    // no-op — exercises the safe-fail path
    [
      { directive_type: 'solar_reduction', applies: false, structured_adjustment: null, notes: '' },
      { directive_type: 'minimum_battery_reserve', applies: false, structured_adjustment: null, notes: '' },
      { directive_type: 'maximum_grid_import', applies: false, structured_adjustment: null, notes: '' },
    ],
    // soft reduction — exercises §5.3.1
    [
      { directive_type: 'solar_reduction', applies: true,
        structured_adjustment: { percent_reduction: 25 },
        notes: 'Soft 25% reduction in solar between 11am-3pm to test inverter derate.' },
    ],
    // window-based max grid cap — exercises §5.3.3
    [
      { directive_type: 'maximum_grid_import', applies: true,
        structured_adjustment: { max_grid_kwh: 3.5, window_start_hour: 17, window_end_hour: 22 },
        notes: 'Cap grid imports at 3.5 kWh from 5pm-10pm to avoid evening peak.' },
    ],
    // battery reserve floor — exercises §5.3.2
    [
      { directive_type: 'minimum_battery_reserve', applies: true,
        structured_adjustment: { reserve_kwh: 6.0 },
        notes: 'Keep battery above 6 kWh overnight to handle next-day startup.' },
    ],
    // multi-directive mix — exercises interaction rules
    [
      { directive_type: 'solar_reduction', applies: true,
        structured_adjustment: { percent_reduction: 40 },
        notes: 'Heavy cloud cover expected — solar capped at 60% of forecast.' },
      { directive_type: 'minimum_battery_reserve', applies: true,
        structured_adjustment: { reserve_kwh: 4.0 },
        notes: 'Reserve 4 kWh for emergency backup.' },
      { directive_type: 'maximum_grid_import', applies: true,
        structured_adjustment: { max_grid_kwh: 5.0, window_start_hour: 18, window_end_hour: 21 },
        notes: 'EV charging pre-empts grid cap during 6-9pm.' },
    ],
  ];

  const directives = randomItem(directiveFamilies);

  return {
    scenario_id: scenarioId,
    site: { name: randomItem(['Block-A', 'Block-B', 'Engineering', 'Library', 'Sports']), timezone: 'Asia/Dhaka' },
    hours,
    battery: {
      capacity_kwh: 10,
      initial_energy_kwh: 5,
      max_charge_kw: 3,
      max_discharge_kw: 3,
      min_reserve_kwh: 1.0,
    },
    tariff_bdt_per_kwh: hours.map((_, h) => (h >= 17 && h <= 22 ? 18 : 8)),
    directives,
  };
}

function pickScenario() {
  if (poolFromFile.length > 0) {
    return randomItem(poolFromFile);
  }
  return makeRandomScenario(`s-${uuidv4()}`);
}

// ---------- HTTP helpers ----------

function postOptimize(body) {
  const params = {
    headers: {
      'Content-Type': 'application/json',
      'Accept': 'application/json',
      'X-Request-ID': `k6-${__VU}-${__ITER}`,
    },
    tags: { endpoint: 'optimize-energy' },
    timeout: '10s', // our SLO is p99 < 2.5 s; 10s leaves headroom for retries
  };
  return http.post(`${BASE_URL}/optimize-energy`, JSON.stringify(body), params);
}

// ---------- Default function ----------

export default function () {
  const body = pickScenario();
  const start = Date.now();
  const res  = postOptimize(body);
  const dur  = Date.now() - start;

  optimizeLatency.add(dur);

  const ok2xx = res.status >= 200 && res.status < 300;
  const is5xx = res.status >= 500 && res.status < 600;
  const is4xx = res.status >= 400 && res.status < 500;

  if (is5xx) serviceErrors.add(1);
  if (is4xx) clientErrors.add(1);

  let usedLLM = false;
  let replayOk = true;
  if (ok2xx && res.body) {
    try {
      const parsed = JSON.parse(res.body);
      usedLLM = parsed.used_llm === true;
      if (parsed.replay_ok === false) replayOk = false;
    } catch (_e) {
      // Body isn't JSON — count as service error so it shows in SLO failure.
      serviceErrors.add(1);
    }
  }

  if (usedLLM) {
    llmPathLatency.add(dur);
    llmInUseRate.add(true);
  } else {
    fallbackLatency.add(dur);
    llmInUseRate.add(false);
  }

  if (!replayOk) replayFailures.add(1);

  check(res, {
    'status is 2xx':       (r) => r.status >= 200 && r.status < 300,
    'latency under 2.5s':  ()  => dur < 2500,
    'replay ok':           ()  => replayOk,
  });

  // Tiny jitter so we don't lock-step across VUs.
  sleep(randomIntBetween(0, 50) / 1000);
}

// ---------- Lifecycle hooks ----------

export function handleSummary(data) {
  // Write a compact summary suitable for CI artifacts.
  return {
    'stdout': textSummary(data, { indent: ' ', enableColors: false }),
    'summary.json': JSON.stringify(data, null, 2),
  };
}

// Minimal text summary (k6's built-in is a heavy bundle; this is enough for CI logs).
function textSummary(data, opts) {
  const m = data.metrics || {};
  const lines = [];
  lines.push('==== GridWise load test summary ====');
  if (m.optimize_latency_ms) {
    const p = m.optimize_latency_ms.values || {};
    lines.push(`optimize_latency_ms: p50=${p['p(50)']?.toFixed(1)} p95=${p['p(95)']?.toFixed(1)} p99=${p['p(99)']?.toFixed(1)} max=${p.max?.toFixed(1)}`);
  }
  if (m.http_reqs) {
    const v = m.http_reqs.values || {};
    lines.push(`http_reqs: count=${v.count} rate=${v.rate?.toFixed(1)}/s`);
  }
  if (m.http_req_failed) {
    lines.push(`http_req_failed rate: ${(m.http_req_failed.values.rate * 100).toFixed(4)}%`);
  }
  if (m.service_errors_total) {
    lines.push(`5xx errors: ${m.service_errors_total.values.count}`);
  }
  if (m.client_errors_total) {
    lines.push(`4xx errors: ${m.client_errors_total.values.count}`);
  }
  if (m.llm_in_use_rate) {
    lines.push(`LLM-path rate: ${(m.llm_in_use_rate.values.rate * 100).toFixed(2)}%`);
  }
  return lines.join('\n') + '\n';
}
