.PHONY: lint fmt test test-unit test-integration check openapi openapi-check \
	docker-up docker-down docker-logs docker-psql \
	helm-lint helm-install helm-upgrade helm-uninstall helm-test \
	smoke smoke-local smoke-k8s

lint:
	uv run ruff check .

fmt:
	uv run ruff format .

test:
	uv run pytest tests/ -x -q

test-unit:
	uv run pytest tests/ -x -q --ignore=tests/integration

test-integration:
	uv run pytest tests/integration/ -x -q

check: lint test-unit openapi-check

openapi:
	uv run python scripts/export_openapi.py

openapi-check:
	uv run python scripts/export_openapi.py
	git diff --exit-code openapi.json || \
		(echo "ERROR: openapi.json is out of date. Run 'make openapi' and commit." && exit 1)

# ── Docker ───────────────────────────────────────────────────────

docker-up:
	docker compose up --build -d

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f app

docker-psql:
	docker compose exec postgres psql -U postgres sleep_harmonizer

# ── Helm ─────────────────────────────────────────────────────────

helm-lint:
	helm lint helm/sleep-harmonizer

PG_DIGEST := sha256:6bb3cb8210a89f19f5d037638901049d7d7c598fbec644455a4dc82fd1c59350

helm-install:
	helm dependency update helm/sleep-harmonizer
	helm install sleep-dev helm/sleep-harmonizer \
		--set image.tag=latest --set image.pullPolicy=Never \
		--set postgresql.image.digest=$(PG_DIGEST)

helm-upgrade:
	helm upgrade sleep-dev helm/sleep-harmonizer \
		--set image.tag=latest --set image.pullPolicy=Never \
		--set postgresql.image.digest=$(PG_DIGEST) --atomic

helm-uninstall:
	helm uninstall sleep-dev

helm-test:
	helm test sleep-dev

# ── Smoke Tests ──────────────────────────────────────────────────

smoke:
	uv run python scripts/smoke_test.py

smoke-local:
	docker compose up --build -d && \
	  ( uv run python scripts/smoke_test.py --wait 60 ; EXIT=$$? ; docker compose down -v ; exit $$EXIT )

smoke-k8s:
	bash -c 'kubectl port-forward svc/sleep-dev-sleep-harmonizer 8000:80 & PF_PID=$$!; \
	  trap "kill $$PF_PID 2>/dev/null" EXIT; \
	  uv run python scripts/smoke_test.py --wait 30 --base-url http://localhost:8000'
