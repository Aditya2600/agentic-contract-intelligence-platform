.PHONY: quickstart install test test-pg api watch db migrate wait-for-db demo demo-crash web up down

# ---------------------------------------------------------------------------
# The one command. Installs, runs the offline test suite, then drives seven
# documents through the whole pipeline -- both human gates included -- with no
# model API key, no GPU and no model server. See README.md.
# ---------------------------------------------------------------------------
quickstart:
	./scripts/quickstart.sh

install:
	python -m pip install -e '.[dev]'

test:
	pytest

test-pg: db migrate
	DOCTASK_TEST_DATABASE_URL=$${DOCTASK_TEST_DATABASE_URL:-postgresql://doctask:doctask@localhost:5432/doctask} pytest

api:
	uvicorn doctask.main:app --reload

# Standalone collection watcher, against whatever DOCTASK_REPOSITORY/DOCTASK_* the
# environment already has set (same variables `make api` uses). Needs
# DOCTASK_WATCHER_TOKEN set to one of DOCTASK_SERVICE_TOKENS.
watch:
	python -m doctask.watcher

db:
	docker compose up -d postgres

# `docker compose up -d` returns as soon as the container starts, which is before
# Postgres is accepting connections. The compose healthcheck already knows the
# difference; this waits on it so `make migrate` cannot lose the race.
wait-for-db:
	@until docker compose exec -T postgres pg_isready -U doctask -d doctask >/dev/null 2>&1; do \
		sleep 1; \
	done

migrate: wait-for-db
	python scripts/apply_migrations.py

# Seven documents, four formats, one register -- entirely offline and deterministic.
demo:
	python scripts/run_demo.py

# Crash recovery, demonstrated rather than asserted: a run is SIGKILLed mid-flight,
# restarted, and the proof is read back out of the stage ledger and the register.
# Needs the durable stack, because a checkpoint only survives a kill in Postgres.
demo-crash: db migrate
	python scripts/crash_demo.py

web:
	cd web && npm install && npm run dev

# Postgres, the API and the watcher together, containerised. `make db && make migrate`
# is the equivalent for running the API/watcher locally against a containerised database.
up:
	docker compose up --build

down:
	docker compose down
