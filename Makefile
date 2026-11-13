-include .env
export

.PHONY: init up down clean test lint migrate demo
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
