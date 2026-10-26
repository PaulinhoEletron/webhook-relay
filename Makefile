.PHONY: init up down test lint migrate demo
init:
	python3 examples/init_env.py
up:
	docker compose up --build -d --wait
down:
	docker compose down
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
