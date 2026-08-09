.PHONY: install test test-pg api db migrate demo web

install:
	python -m pip install -e '.[dev]'

test:
	pytest

test-pg:
	DOCTASK_TEST_DATABASE_URL=$${DOCTASK_TEST_DATABASE_URL:-postgresql://doctask:doctask@localhost:5432/doctask} pytest

api:
	uvicorn doctask.main:app --reload

db:
	docker compose up -d postgres

migrate:
	python scripts/apply_migrations.py

demo:
	python scripts/run_demo.py

web:
	cd web && npm install && npm run dev
