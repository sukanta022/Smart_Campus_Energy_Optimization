# GridWise — Operations Runbook

Audience: the engineer paged at 03:00. Assumes AWS, ECS Fargate, GitHub Actions, and that you've skimmed `README.md`.

---

## 1. Architecture at a glance

| Layer | Component | Where |
|-------|-----------|-------|
| Edge | WAF v2 (Common + KnownBadInputs + per-IP rate cap 1500/5min) | `infra/waf.tf` |
| Edge | ALB (HTTPS termination, access logs to S3) | `infra/alb.tf` |
| Compute | ECS Fargate + FARGATE_SPOT (4:1 weight) | `infra/ecs.tf` |
| State | ElastiCache Redis (rate-limit middleware) | `infra/redis.tf` |
| Secrets | Secrets Manager (OPENAI_API_KEY, KMS-encrypted) | `infra/secrets.tf` |
| Image | ECR repo (scan-on-push, lifecycle 7d untagged / 30 release-*) | `infra/ecr.tf` |
| Logs | CloudWatch `/ecs/gridwise-*` (retention 30d prod / 7d dev) | `infra/logs.tf` |
| Alerts | SNS topic `${name_prefix}-alerts` (5xx alarm) | `infra/logs.tf` |
| Scaling | Application Auto Scaling (CPU 60% / Mem 70% / ALB 200 RPS/task) | `infra/ecs.tf` |
| CI | Lint + typecheck + test + Trivy fs + docker-build + tf-validate | `.github/workflows/ci.yml` |
| CD | OIDC assume role → ECR push → ECS rolling deploy + `/health` smoke | `.github/workflows/deploy.yml` |

---

## 2. Deploys

### 2.1 Routine deploy (tag-based)

```bash
git checkout main && git pull
# bump version in CHANGELOG.md (if present)
git tag v0.2.0 && git push --tags
```

GitHub Actions will:
1. Resolve tag → `image_tag=v0.2.0`.
2. Assume `gridwise-prod-gha-deploy` role via OIDC.
3. `docker buildx` + push to ECR (immutable tags `v0.2.0` *and* `sha-<git-sha>`).
4. Trivy image scan (fails on CRITICAL).
5. Register a new ECS task definition, update service, wait `services-stable`.
6. Poll `https://<alb-dns>/health` for 5 attempts with backoff.

Watch the **deploy** workflow run. If it succeeds and `services-stable` reports `ROLLING_COMPLETED`, you're done.

### 2.2 Routine deploy (main branch)

Pushes to `main` reuse the same pipeline but tag the image with the short SHA.

### 2.3 Manual deploy

`Actions → deploy → Run workflow → ref=main` (or any branch). Useful for canary retries without merging.

---

## 3. Rollback

ECS keeps the last three task definitions by default; we don't change that. To roll back:

```bash
# Find the previous task def ARN
aws ecs list-task-definitions \
  --family-prefix gridwise-prod \
  --sort DESC --max-items 5

# Pick the one before the bad revision, e.g. arn:aws:ecs:...:task-definition/gridwise-prod:42
PREV=arn:aws:ecs:REGION:ACCOUNT_ID:task-definition/gridwise-prod:42

# Update service to use it (rolling deploy)
aws ecs update-service --cluster gridwise-prod \
  --service gridwise-prod --task-definition "$PREV"
```

Watch:

```bash
aws ecs describe-services --cluster gridwise-prod --services gridwise-prod \
  --query 'services[0].deployments'
```

When `ROLLOUT_STATE=COMPLETED` for the older revision and only one deployment is left, you're on the old code.

For an **emergency stop-the-bleed** rollback, set desired count to 0, then redeploy a known-good image:

```bash
aws ecs update-service --cluster gridwise-prod --service gridwise-prod \
  --desired-count 0
```

---

## 4. On-call escalation

| Severity | Trigger | First action |
|----------|---------|--------------|
| **P1** | 5xx alarm fires (>=10 errors in 60s for 2 periods) OR `/health` returning 503 for >2 min | Page secondary, check SNS topic `${name_prefix}-alerts` |
| **P2** | p99 `/optimize-energy` latency > 5 s sustained 5 min OR `replay_failures_total` > 0 | Open ticket, no page |
| **P3** | Directives validator noise (`directive_validation_failures_total`) > 5% over 30 min | Inspect `/metrics` for `directive_type` label skew |

### 4.1 CloudWatch dashboards

- `/ecs/gridwise-*` log group: live tail of structured JSON
- `GridWise-5xx` metric: triggered by `status_code >= 500` pattern filter
- Application Auto Scaling metrics: `ECSServiceAverageCPUUtilization`, `ECSServiceAverageMemoryUtilization`, `TargetResponseTime`, `RequestCountPerTarget`

### 4.2 Logs

```bash
# Last 200 log lines from the running task
aws logs tail /ecs/gridwise-prod --follow --format short

# Only errors
aws logs tail /ecs/gridwise-prod --follow --format short \
  --filter-pattern '{ $.level = "error" }'
```

---

## 5. Scaling

Application Auto Scaling keeps three target-tracking policies on the ECS service:

| Policy | Target | Cooldown |
|--------|--------|----------|
| CPU | 60% | 60 s scale-out, 300 s scale-in |
| Memory | 70% | 60 s scale-out, 300 s scale-in |
| ALB request count | 200 RPS per task | 60 s scale-out, 300 s scale-in |

Manual nudge (off-hours, demo day, etc.):

```bash
aws application-autoscaling register-scalable-target \
  --service-namespace ecs \
  --resource-id service/gridwise-prod/gridwise-prod \
  --scalable-dimension ecs:service:DesiredCount \
  --min-capacity 3 --max-capacity 10
```

If you need to bypass Auto Scaling entirely (e.g. tuning experiments):

```bash
# Suspend each policy
for p in cpu memory alb_rps; do
  aws application-autoscaling suspend-scheduled-action \
    --service-namespace ecs \
    --resource-id service/gridwise-prod/gridwise-prod \
    --scalable-dimension ecs:service:DesiredCount \
    --scheduled-action-name "$p" 2>/dev/null || true
done
```

---

## 6. Incident playbooks

### 6.1 LLM provider outage (OpenAI 5xx)

**Symptoms**: `gridwise_llm_latency_seconds{outcome="breaker_open"}` spiking, request success rate drops because every request needs to wait through 3 retries.

**Diagnosis**: `/metrics` shows breaker open. Logs contain `"circuit_breaker_open"` errors.

**Mitigation**:
1. The fallback regex parser auto-takes over after 3 consecutive LLM failures (see `src/llm/interpreter.py` + `src/app/resilience.py`). Coverage may drop but the service stays up.
2. If the fallback is insufficient, set `GRIDWISE_ENABLE_FALLBACK_PARSER=true` (it's already on by default).
3. Consider a circuit-breaker trip threshold bump via env: `GRIDWISE_BREAKER_FAIL_MAX` (default 5).

**After**: clear incidents in OpenAI's status page; breakers will half-open automatically and recover.

### 6.2 High p99 latency

**Symptoms**: `http_req_duration` p99 > 2.5 s on k6, OR `optimize_latency_ms` p99 > 2.5 s in `/metrics`.

**Diagnosis**:
1. Are we CPU-bound? Check `ECSServiceAverageCPUUtilization`. > 70% = scale out.
2. Are we rate-limited? Look for `gridwise_requests_total{outcome="429"}` non-zero.
3. Are we slow in the LP? `gridwise_optimizer_latency_seconds` p99 should be < 200 ms. If it's much higher, an oversized `capacity_kwh` or pathological demand shape is at fault.
4. Are we slow in the LLM? `gridwise_llm_latency_seconds{outcome="ok"}` p99. > 2 s = OpenAI is congested.

**Mitigations**:
1. Force a scale-out (section 5).
2. Tighten `GRIDWISE_RATE_LIMIT_PER_MINUTE` if origin is per-IP (we're already on a fixed window).
3. Force the fallback parser (`GRIDWISE_FORCE_FALLBACK=true`) to bypass OpenAI until things calm down.

### 6.3 Replay failures

**Symptoms**: `gridwise_judge_replay_failures_total > 0`.

**Why**: The replay checker (`src/app/replay.py`) re-derives the response and asserts the LP produced the same numbers within `TOLERANCE_KWH = 0.01`. If they disagree, something is inconsistent.

**Diagnosis**: Look at the log line tagged with `event="replay_mismatch"`. It prints `request_id` and which hour(s) drift.

**Mitigation**: Treat as **a bug** — page the team. Likely culprits are LP warm-start arithmetic drift or rounding in the directive applicator. Open a hotfix; do not silence the alarm.

### 6.4 Bad directives from the LLM

**Symptoms**: `gridwise_directive_validation_failures_total` rises.

**Why**: The validator caught a bad directive from the LLM and clamped it to `no_op`. The system *will* still return a valid plan; the directive just got ignored.

**Diagnosis**: Logs contain `event="directive_validation_failed"` with the original directive. The `directive_type` Prometheus label tells you which class is breaking (e.g. `maximum_grid_import`).

**Mitigation**: This is normal — the whole point of the validator is to absorb LLM drift. If it's noisy, update the system prompt in `src/llm/prompts/system.txt` or tighten the `response_format` schema in `src/llm/schema.py`.

---

## 7. Local development

```bash
python -m pip install -e ".[dev]"
export PYTHONPATH=src
python -m uvicorn app.main:app --reload --port 8080
```

Run tests:

```bash
pytest tests/unit -v
pytest tests/integration -v
```

Load test (requires k6 + a running service):

```bash
k6 run -e BASE_URL=http://localhost:8080 tests/load/k6_optimize.js
```

---

## 8. Useful one-liners

```bash
# Service status
aws ecs describe-services --cluster gridwise-prod \
  --services gridwise-prod --query 'services[0].{desired:desiredCount,running:runningCount,pending:pendingCount}'

# Currently running task IPs (for SSH-equivalent debugging via ECS Exec)
aws ecs list-tasks --cluster gridwise-prod --service-name gridwise-prod \
  --query 'taskArns[]' --output text | xargs -n1 -I{} \
  aws ecs describe-tasks --cluster gridwise-prod --tasks {} \
  --query 'tasks[0].containers[0].runtimeId'

# Force a new deployment with the same image (e.g. to pick up a fresh task role)
aws ecs update-service --cluster gridwise-prod --service gridwise-prod \
  --force-new-deployment

# Rotate the OpenAI API key
NEW_KEY=sk-...
aws secretsmanager put-secret-value \
  --secret-id gridwise/prod/openai-api-key \
  --secret-string "$NEW_KEY"
# Then force a redeploy so tasks pick up the new env var (secrets are read at task start).
```

---

## 9. Post-incident

After every P1/P2: write a one-page blurb (timeline, root cause, customer impact, action items) in `docs/postmortems/<date>-<slug>.md`. The Replay-failure and 5xx-alarm paths are the most common seeds for a postmortem.
