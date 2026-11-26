-include .env
export

# Image tag from the pyproject version; git SHA recorded as an image label.
RELAY_VERSION := $(shell sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1)
GIT_SHA := $(shell git rev-parse --short HEAD 2>/dev/null || echo unknown)
export RELAY_VERSION GIT_SHA

.PHONY: init up down clean test lint migrate demo e2e test-browser backup restore rollback
init:
	python3 examples/init_env.py
up:
	docker compose up --build -d --wait
# Stops the stack and the test database container; keeps the data volume.
down:
	docker compose --profile test down
# Destructive: also deletes the database volume (all local data).
clean:
	docker compose --profile test down -v
migrate:
	docker compose run --rm migrate
test:
	docker compose --profile test build test
	docker compose --profile test run --rm test
lint:
	uv run --frozen ruff check .
	uv run --frozen ruff format --check .
demo:
	docker compose exec -T api python examples/producer.py
# Full scenario run against the stack (starts it if needed); wait 60 s between runs (demo quota).
e2e:
	scripts/e2e.sh
# Browser checks need a running stack. Set PLAYWRIGHT_CHROMIUM_EXECUTABLE to use an installed
# Chromium; otherwise run `npx playwright install chromium` once.
test-browser:
	npm ci
	EXPECT_SOURCE_URL="$(SOURCE_URL)" npm run test:browser
# Streams pg_dump to backups/relay-<UTC timestamp>.dump and keeps the newest 7.
backup:
	scripts/backup.sh
# Replaces ALL data from a dump: make restore FILE=backups/relay-<timestamp>.dump [CONFIRM=yes]
restore:
	scripts/restore.sh "$(FILE)"
# Runs an older git ref against the existing data: make rollback VERSION=<tag or commit> [CONFIRM=yes]
rollback:
	scripts/rollback.sh "$(VERSION)"
