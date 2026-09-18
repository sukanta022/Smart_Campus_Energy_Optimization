"""Prometheus metrics for the GridWise LLM service."""
from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram


# A dedicated registry lets us scope metrics cleanly and avoid double-registering
# during tests (the default global registry collides on hot reload).
REGISTRY = CollectorRegistry()


llm_latency_seconds = Histogram(
    "gridwise_llm_latency_seconds",
    "Latency of OpenAI chat completion in seconds.",
    labelnames=("outcome",),  # success | fallback | error
    buckets=(0.1, 0.25, 0.5, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0),
    registry=REGISTRY,
)

optimizer_latency_seconds = Histogram(
    "gridwise_optimizer_latency_seconds",
    "Latency of PuLP solve in seconds.",
    labelnames=("status",),
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0),
    registry=REGISTRY,
)

replay_failures_total = Counter(
    "gridwise_judge_replay_failures_total",
    "Count of schedule replays that failed PRD §11.3 checks.",
    registry=REGISTRY,
)

directive_validation_failures = Counter(
    "gridwise_directive_validation_failures_total",
    "Count of directive entries rejected by the validator.",
    labelnames=("directive_type",),
    registry=REGISTRY,
)

requests_total = Counter(
    "gridwise_requests_total",
    "Total /optimize-energy requests handled.",
    labelnames=("outcome",),  # ok | 4xx | 5xx
    registry=REGISTRY,
)

llm_in_use = Gauge(
    "gridwise_llm_in_use",
    "1 if the LLM path served the request, 0 if the fallback parser was used.",
    registry=REGISTRY,
)


__all__ = [
    "REGISTRY",
    "llm_latency_seconds",
    "optimizer_latency_seconds",
    "replay_failures_total",
    "directive_validation_failures",
    "requests_total",
    "llm_in_use",
]
