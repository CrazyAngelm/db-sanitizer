SHELL := /bin/sh

UV := uv run
COMPOSE := docker compose --env-file .env
DOCKER := docker
OLLAMA_VOLUME := db-sanitizer-ollama-data
DEMO_RUN_ID ?= demo
OLLAMA_DEMO_RUN_ID ?= demo-ollama
OPENROUTER_DEMO_RUN_ID ?= demo-openrouter
OLLAMA_BOOTSTRAP_RETRIES ?= 3
PERF_ROWS ?= 100000
PERF_RUN_ID := perf-$(PERF_ROWS)

.PHONY: demo demo-ollama demo-openrouter test test-integration test-all lint clean clean-ollama perf

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

# Default demo: local Ollama, synthetic PostgreSQL data, and a fresh run directory.
demo: clean
	@test -f .env || (echo "Create .env from .env.example first" >&2; exit 2)
	@$(DOCKER) volume create $(OLLAMA_VOLUME) >/dev/null
	@attempt=1; until $(COMPOSE) --profile ollama up -d --build --wait source-db target-db ollama; do \
		if [ $$attempt -ge $(OLLAMA_BOOTSTRAP_RETRIES) ]; then exit 1; fi; \
		echo "Ollama bootstrap failed; retrying ($$attempt/$(OLLAMA_BOOTSTRAP_RETRIES))..." >&2; \
		attempt=$$((attempt + 1)); sleep 5; \
	done
	@attempt=1; until $(COMPOSE) --profile ollama run --rm --no-deps ollama-pull; do \
		if [ $$attempt -ge $(OLLAMA_BOOTSTRAP_RETRIES) ]; then exit 1; fi; \
		echo "Ollama model pull failed; retrying ($$attempt/$(OLLAMA_BOOTSTRAP_RETRIES))..." >&2; \
		attempt=$$((attempt + 1)); sleep 5; \
	done
	$(COMPOSE) --profile ollama run --rm --build --no-deps sanitizer run --policy config/policy.demo.yaml --run-id $(DEMO_RUN_ID)

# Backward-compatible name for the same local Ollama scenario.
demo-ollama: DEMO_RUN_ID = $(OLLAMA_DEMO_RUN_ID)
demo-ollama: demo

# Optional remote-provider scenario; it is never used by the default demo.
demo-openrouter: clean
	@test -f .env || (echo "Create .env from .env.example first" >&2; exit 2)
	$(COMPOSE) up -d --build --wait source-db target-db
	$(COMPOSE) run --rm --build --no-deps sanitizer run --policy config/policy.openrouter.yaml --run-id $(OPENROUTER_DEMO_RUN_ID)

# Uses the explicit in-process test provider to isolate data-plane throughput from API latency/cost.
perf: clean
	@test -f .env || (echo "Create .env from .env.example first" >&2; exit 2)
	$(COMPOSE) up -d --build --wait source-db target-db
	$(COMPOSE) run --rm --build --no-deps --entrypoint python sanitizer demo/seed_generator.py --rows $(PERF_ROWS)
	$(COMPOSE) run --rm --no-deps -e DB_SANITIZER_USE_FAKE_PROVIDER=1 sanitizer run --policy config/policy.demo.yaml --run-id $(PERF_RUN_ID)
	$(UV) python scripts/write_benchmark.py --run-dir .runs/$(PERF_RUN_ID) --output perf-results/benchmark-$(PERF_ROWS).json --requested-rows $(PERF_ROWS)

clean:
	@if [ -f .env ]; then $(COMPOSE) --profile ollama --profile test down --volumes --remove-orphans; fi
	$(UV) python scripts/clean.py

# Explicitly remove the cached local Ollama model; normal clean keeps it for fast retries.
clean-ollama: clean
	@$(DOCKER) volume rm -f $(OLLAMA_VOLUME) >/dev/null 2>&1 || true
