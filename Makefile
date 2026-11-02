# Common developer commands. Run `make help`.
.PHONY: help up down demo stream logs test test-backend test-frontend lint smoke evaluate

help:
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-15s %s\n", $$1, $$2}'

up: ## Build and start the full stack (http://localhost:8080)
	docker compose up -d --build

down: ## Stop the stack (keeps data volumes)
	docker compose down

demo: ## Demo users + a simulated year of transactions + train + backfill + drift report
	docker compose run --rm api python -m app.cli demo

stream: ## Publish two minutes of live synthetic transactions to Kafka (scored by the stream scorer)
	docker compose run --rm api python -m app.cli simulate-stream --rate 20 --seconds 120

logs: ## Follow API, worker and stream scorer logs
	docker compose logs -f api worker scorer

test: test-backend test-frontend ## Run all tests

test-backend: ## Backend tests (needs PostgreSQL; see docs/DEVELOPMENT.md)
	cd backend && pytest

test-frontend: ## Frontend unit tests
	cd frontend && npm test

lint: ## Lint and type-check everything
	cd backend && ruff check app tests migrations && ruff format --check app tests migrations && mypy app
	cd frontend && npm run lint && npm run typecheck

smoke: ## Browser smoke test against the running stack (needs E2E_PASSWORD)
	cd e2e && npm install --no-audit --no-fund && node smoke.cjs

evaluate: ## Re-run the offline model evaluation and regenerate ml/reports/
	cd backend && python -m app.cli evaluate --out ../ml/reports
