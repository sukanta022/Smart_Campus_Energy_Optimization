# GridWise — Submission Walkthrough

**Hackathon**: BUP CSE FEST 2026 — Preliminary Round
**Project**: LLM-orchestrated energy optimization service
**Tagline**: *Operators write English. Physics stays deterministic.*

This document maps every requirement in the PRD to the code that satisfies it. Each section starts with the PRD paragraph reference and ends with the concrete files you can open.

---

## §01 Mission

> *Build an intelligent system that optimizes campus energy usage by orchestrating an LLM to interpret human directives, applying physics-based scheduling constraints, and producing actionable plans.*

**Satisfied by**: the entire repo, but especially `src/app/orchestrator.py` (the end-to-end pipeline) and `README.md` (high-level pitch).

---

## §02 Goals

> *Reduce peak demand, integrate renewables, respect operator instructions, scale to campus-wide deployments.*

| Goal | Where it lives |
|------|----------------|
| Reduce peak demand | `src/optimizer/scheduler.py` — peak-aware cost weights in the LP objective |
| Integrate renewables | `src/optimizer/scheduler.py` — `effective_solar[h]` derived from directives, free energy preferred in the LP |
| Respect operator instructions | `src/llm/validator.py` + `src/optimizer/directives.py` — directive → EffectiveArrays → hard LP constraint |
| Scale to campus-wide deployments | `infra/ecs.tf` — Fargate + Auto Scaling (CPU 60% / Mem 70% / 200 ALB RPS per task) |

---

## §03–§06 Concepts

> *Hour-of-day modeling, kWh units, demand/solar/tariff arrays, battery as buffer with SoC.*

**Satisfied by**:

- `src/app/models.py` — typed Pydantic models for `Demand`, `SolarForecast`, `Tariff`, `Battery`, `SystemRequest`, `SystemResponse`. All arrays are length 24 with float kWh.
- `src/optimizer/scheduler.py` — 24-hour horizon, end-of-day SoC equality constraint, end-of-day neutrality enforced.
- `src/optimizer/directives.py` — `EffectiveArrays` namespace encapsulates per-hour effective values for solar, max_grid, min_reserve, charge/discharge windows.

---

## §07 Request shape

> *The system must accept a JSON request with hours, demand, solar, tariff, battery, and operator directives.*

**Satisfied by**:

- `src/app/models.py` — `SystemRequest` Pydantic model with fields `hours: list[int]`, `demand_kwh: list[float]`, `solar_forecast_kwh: list[float]`, `tariff_usd_per_kwh: list[float]`, `battery: BatterySpec`, `directives: list[OperatorDirective]`. Length-24 arrays are validated.
- `tests/load/scenarios.json` — 4 fixtures with the exact §07 shape (PRD-pool-01 … PRD-pool-04).
- `src/app/main.py` — `POST /optimize-energy` parses via `SystemRequest.model_validate_json(...)`.

---

## §08 Guardrails

> *The LLM may hallucinate units, drop entries, or contradict itself. The system must clamp, range-check, and silently no-op bad directives without crashing.*

**Satisfied by**:

- `src/llm/validator.py` — directive-level validators (`validate_directive`) for each directive type (`minimum_battery_reserve`, `maximum_grid_import`, `solar_reduction`, `charge_window`, `discharge_window`, `no_op`). Range, type, hour-window sanity, kWh non-negativity.
- `src/llm/interpreter.py` — JSON parse failures, schema mismatches, and validator rejections each produce a `used_llm=False` response and fall through to the deterministic fallback.
- `src/app/main.py` — top-level `RateLimitExceeded` middleware translates to 429, never crashes the request loop.
- `tests/load/scenarios.json` — `PRD-pool-04-oversize-battery-cap` deliberately uses `reserve_kwh=99`; the validator clamps to capacity without rejecting the request.

---

## §09 LLM interpretation

> *Translate natural-language directives into a structured JSON list of typed directives.*

**Satisfied by**:

- `src/llm/interpreter.py` — `AsyncOpenAI` client with `temperature=0`, `seed=42`, `response_format={"type":"json_schema", ...}`.
- `src/llm/schema.py` — JSON Schema definition for the directive list. Every field is typed (no free strings).
- `src/llm/prompts/system.txt` — system prompt explicitly enumerates the allowed directive types and their units.
- `src/llm/fallback.py` — deterministic regex parser used when (a) `OPENAI_API_KEY` is absent, (b) the circuit breaker is open, or (c) `GRIDWISE_FORCE_FALLBACK=true`. Picks the same shape, so downstream code is identical.
- `src/app/response.json` (`used_llm: bool`) — every response tells the caller whether the LLM path was actually used.

---

## §10 Response shape

> *For each hour: grid_kwh, solar_used_kwh, charge_kwh, discharge_kwh, soc_kwh. Plus totals and metadata.*

**Satisfied by**:

- `src/app/models.py` — `SystemResponse` with `plan: list[HourlyPlan]`, `cost_usd: float`, `directives_applied: list[DirectiveRecord]`, `used_llm: bool`, `replay_ok: bool`.
- `src/optimizer/scheduler.py` — response builder rounds to `TOLERANCE_KWH = 0.01` and emits per-hour entries plus end-of-day SoC delta.
- `tests/load/k6_optimize.js` — assertion checks every response has 24 plan entries and `replay_ok=true`.

---

## §11 Robustness

### §11.1 LLM provider errors
- `src/llm/interpreter.py` — `tenacity` retry decorator with exponential backoff (max 3 attempts, jitter).
- `src/app/resilience.py` — `pybreaker.CircuitBreaker` (fail_max=5, reset timeout 60 s) wrapping the LLM call. While open, the request goes straight to the fallback.

### §11.2 Optimizer infeasibility
- `src/optimizer/scheduler.py` — PuLP returns `LpStatus`; anything other than `Optimal` raises `InfeasiblePlanError` (handled as HTTP 500 by `src/app/main.py`).

### §11.3 Judge replay
- `src/app/replay.py` — re-runs the optimizer from the validated directive list and asserts the per-hour arrays match within `TOLERANCE_KWH = 0.01`. If the validator silently dropped or mutated a directive differently than expected, the replay will catch it and the request returns 500.
- `src/app/main.py` — replay check is the last step before serialization.

---

## §12 Latency

> *SLO: p99 < 2.5 s under 1 k req/s for 10 minutes; error rate < 0.1%; ≥ 600 k requests served.*

**Satisfied by**:

- `tests/load/k6_optimize.js` — `constant-arrival-rate` executor at 1000 RPS for 10 minutes; thresholds enforce the SLO directly:
  - `optimize_latency_ms: p(99) < 2500`
  - `http_req_failed: rate < 0.001`
  - `http_reqs: count >= 600000`
  - `service_errors_total: count < 60`
- `src/app/main.py` — `uvicorn` with `--loop uvloop --http httptools --proxy-headers` for low-overhead HTTP.
- `src/llm/interpreter.py` — `asyncio.gather` ready; single concurrent in-flight per request but the LLM call is the longest pole.
- `infra/waf.tf` — 1500 req / 5 min per-IP rate cap as a coarse back-pressure.
- `infra/ecs.tf` — Auto Scaling on CPU 60% / Memory 70% / ALB 200 RPS per task keeps p99 stable as load grows.

---

## §13 Observability

> *Provide request metrics, latency histograms, LLM vs fallback usage, directive-validation noise, replay failures.*

**Satisfied by**:

- `src/app/metrics.py` — `prometheus_client` custom registry with:
  - `gridwise_llm_latency_seconds` (Histogram, label `outcome={ok,error,breaker_open}`)
  - `gridwise_optimizer_latency_seconds` (Histogram, label `status={ok,error}`)
  - `gridwise_judge_replay_failures_total` (Counter)
  - `gridwise_directive_validation_failures_total` (Counter, label `directive_type`)
  - `gridwise_requests_total` (Counter, label `outcome={ok,400,500,429}`)
  - `gridwise_llm_in_use` (Gauge)
- `src/app/main.py` — `GET /metrics` returns the standard Prometheus exposition.
- `src/app/logging_setup.py` — `structlog` with JSON renderer; every request emits `event="request_completed"` with `request_id`, `latency_ms`, `used_llm`, `replay_ok`, and per-hour `cost_usd`.
- `infra/logs.tf` — `/ecs/gridwise-*` log group (KMS-encrypted, 30 d retention prod) + CloudWatch metric filter on `status_code >= 500` → SNS alarm.
- `tests/load/k6_optimize.js` — additional custom Trend/Rate/Counter metrics: `optimizeLatency`, `llmPathLatency`, `fallbackLatency`, `serviceErrors`, `llmInUseRate` (visible in `summary.json`).

---

## §14 Deployment & CI/CD

**Satisfied by**:

- `Dockerfile` — multi-stage `python:3.12-slim`; non-root `gridwise` user (UID 10001); tini init; uvicorn with uvloop + httptools; healthcheck against `/health`.
- `.dockerignore` — strips `.git`, tests, IDE caches, docs.
- `infra/*.tf` — 14-file IaC: VPC (2-AZ, NAT, gateway + interface endpoints), ECR (scan-on-push, KMS, lifecycle), Secrets Manager (CMK), IAM (task exec/role + GitHub OIDC), security groups, ElastiCache (KMS at-rest + TLS in-transit), ALB (access logs, optional HTTPS), WAF v2 (Common + KnownBadInputs + 1500/5min per-IP), CloudWatch logs + 5xx alarm, ECS Fargate + FARGATE_SPOT + Auto Scaling.
- `.github/workflows/ci.yml` — lint (ruff), typecheck (mypy --strict), test (pytest with coverage gate ≥80%), security-scan (Trivy fs SARIF), docker-build, terraform-validate.
- `.github/workflows/deploy.yml` — OIDC assume role → ECR push → Trivy image scan → register task definition → ECS rolling deploy → poll `/health` smoke.

---

## §15 Documentation

- `README.md` — overview, quickstart, architecture diagram, repo layout, dev/deploy instructions.
- `RUNBOOK.md` — deploy/rollback, on-call escalation, scaling knobs, incident playbooks (LLM outage, high p99, replay failure, bad-directive noise), useful one-liners.
- `SUBMISSION.md` — this document.

---

## Appendix A — How to verify (judge script)

```bash
# 1. Static checks
python -m pip install -e ".[dev]"
ruff check src tests
mypy src --strict
pytest tests/unit tests/integration -v

# 2. Live API
export PYTHONPATH=src
export OPENAI_API_KEY=${OPENAI_API_KEY:-test}        # forces fallback path
python -m uvicorn app.main:app --port 8080 &
sleep 2
curl -fsS http://localhost:8080/health
curl -fsS -X POST http://localhost:8080/optimize-energy \
  -H 'Content-Type: application/json' \
  -d @tests/load/scenarios.json | jq .[0]
pkill -f 'uvicorn app.main'
```

The first request should return `200` with `used_llm=false` (no real key) and `replay_ok=true`. With a real `OPENAI_API_KEY`, the same request will return `used_llm=true` and *also* pass replay.

---

## Appendix B — File map

```
README.md, RUNBOOK.md, SUBMISSION.md       # docs
Dockerfile, .dockerignore                   # container
pyproject.toml                              # deps + ruff/mypy config

src/app/                                    # service
  main.py, orchestrator.py, models.py,
  config.py, metrics.py, logging_setup.py,
  resilience.py, replay.py

src/llm/                                    # LLM interpretation
  interpreter.py, schema.py, validator.py,
  fallback.py, prompts/system.txt

src/optimizer/                              # deterministic planning
  scheduler.py, directives.py

infra/                                      # 14 Terraform files
  main.tf, variables.tf, outputs.tf,
  vpc.tf, ecr.tf, secrets.tf, iam.tf,
  security_groups.tf, redis.tf, alb.tf,
  waf.tf, logs.tf, ecs.tf,
  backend.hcl.example, terraform.tfvars.example

.github/workflows/
  ci.yml, deploy.yml                        # CI + CD

tests/
  unit/                                     # validator/scheduler/directives tests
  integration/                              # end-to-end FastAPI tests
  load/
    k6_optimize.js, scenarios.json,
    package.json
```
