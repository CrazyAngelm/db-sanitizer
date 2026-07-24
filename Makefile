SHELL := /bin/sh

UV := uv run
COMPOSE := docker compose --env-file .env
DEMO_RUN_ID ?= demo
OLLAMA_DEMO_RUN_ID ?= demo-ollama
OLLAMA_MODEL ?= qwen3:4b
PERF_ROWS ?= 100000
PERF_RUN_ID := perf-$(PERF_ROWS)

.PHONY: demo demo-ollama test test-integration test-all lint clean perf

test:
	$(UV) pytest -m "not integration"

# Recreates demo databases, then runs the Greenmask-capable integration image.
test-integration: clean
	@test -f .env || (echo "Create .env from .env.example first" >&2; exit 2)
	$(COMPOSE) --profile test up -d --build --wait source-db target-db
	$(COMPOSE) --profile test run --rm --build --no-deps sanitizer-test

# Complete local gate: offline unit/security tests plus Docker integration tests.
test-all:
	$(UV) pytest -m "not integration"
	$(MAKE) test-integration

lint:
	$(UV) ruff check src tests demo scripts
	$(UV) ruff format --check src tests demo scripts

# Always starts from fresh synthetic source/target volumes and a new run directory.
demo: clean
	@test -f .env || (echo "Create .env from .env.example first" >&2; exit 2)
	$(COMPOSE) up -d --build --wait source-db target-db
	$(COMPOSE) run --rm --build --no-deps sanitizer run --policy config/policy.demo.yaml --run-id $(DEMO_RUN_ID)

# Optional local-model path; default make demo remains OpenRouter-backed.
demo-ollama: clean
	@test -f .env || (echo "Create .env from .env.example first" >&2; exit 2)
	OLLAMA_MODEL=$(OLLAMA_MODEL) $(COMPOSE) --profile ollama up -d --build --wait source-db target-db ollama
	OLLAMA_MODEL=$(OLLAMA_MODEL) $(COMPOSE) --profile ollama run --rm --no-deps ollama-pull
	$(COMPOSE) --profile ollama run --rm --build --no-deps sanitizer run --policy config/policy.ollama.yaml --run-id $(OLLAMA_DEMO_RUN_ID)

# Uses the explicit in-process test provider to isolate data-plane throughput from API latency/cost.
perf: clean
	@test -f .env || (echo "Create .env from .env.example first" >&2; exit 2)
	$(COMPOSE) up -d --build --wait source-db target-db
	$(COMPOSE) run --rm --build --no-deps --entrypoint python sanitizer demo/seed_generator.py --rows $(PERF_ROWS)
	$(COMPOSE) run --rm --no-deps -e DB_SANITIZER_USE_FAKE_PROVIDER=1 sanitizer run --policy config/policy.demo.yaml --run-id $(PERF_RUN_ID)
	$(UV) python scripts/write_benchmark.py --run-dir .runs/$(PERF_RUN_ID) --output perf-results/benchmark-$(PERF_ROWS).json --requested-rows $(PERF_ROWS)

clean:
	@if [ -f .env ]; then $(COMPOSE) down --volumes --remove-orphans; fi
	$(UV) python scripts/clean.py
