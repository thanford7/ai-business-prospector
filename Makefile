.DEFAULT_GOAL := help

ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
BACKEND := $(ROOT)/backend

# pyproject.toml, the virtualenv, and alembic.ini live in backend/.
# --directory also sets the working directory so Alembic and pytest find them.
UV := uv --directory $(BACKEND)
COMPOSE := docker compose
ALEMBIC := $(UV) run alembic

# Alembic reads DATABASE_URL from the environment. Compose loads the root .env on its own.
load_env = set -a; [ -f $(ROOT)/.env ] && . $(ROOT)/.env; set +a;

.PHONY: help sync lock upgrade add add-dev remove remove-dev tree test serve \
	up down stop logs ps restart migrate downgrade revision current history heads check \
	ruff format format-check mypy bandit lint

help: ## Show available commands
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

sync: ## Install dependencies into the project environment
	$(UV) sync

lock: ## Refresh uv.lock without upgrading versions
	$(UV) lock

upgrade: ## Upgrade dependencies and refresh uv.lock
	$(UV) lock --upgrade

add: ## Add a dependency (make add pkg=name)
	@test -n "$(pkg)" || (echo 'Usage: make add pkg=name' && exit 1)
	$(UV) add $(pkg)

add-dev: ## Add a dev dependency (make add-dev pkg=name)
	@test -n "$(pkg)" || (echo 'Usage: make add-dev pkg=name' && exit 1)
	$(UV) add --dev $(pkg)

remove: ## Remove a dependency (make remove pkg=name)
	@test -n "$(pkg)" || (echo 'Usage: make remove pkg=name' && exit 1)
	$(UV) remove $(pkg)

remove-dev: ## Remove a dev dependency (make remove-dev pkg=name)
	@test -n "$(pkg)" || (echo 'Usage: make remove-dev pkg=name' && exit 1)
	$(UV) remove --dev $(pkg)

tree: ## Show the dependency tree
	$(UV) tree

test: ## Run tests against the test database (override with args="-k name")
	$(load_env) $(UV) run pytest $(args)

serve: ## Run the API and scheduler in one process
	$(load_env) $(UV) run uvicorn main:app --host 127.0.0.1 --port 8000

up: ## Start services in the background
	$(COMPOSE) up -d

down: ## Stop services and remove containers
	$(COMPOSE) down

stop: ## Stop services without removing containers
	$(COMPOSE) stop

logs: ## Follow service logs
	$(COMPOSE) logs -f

ps: ## Show service status
	$(COMPOSE) ps

restart: ## Restart services
	$(COMPOSE) restart

migrate: ## Apply migrations through head
	$(load_env) $(ALEMBIC) upgrade head

downgrade: ## Roll back one revision (override with rev=<target>)
	$(load_env) $(ALEMBIC) downgrade $(or $(rev),-1)

revision: ## Autogenerate a migration (make revision m="message")
	@test -n "$(m)" || (echo 'Usage: make revision m="message"' && exit 1)
	$(load_env) $(ALEMBIC) revision --autogenerate -m "$(m)"

current: ## Show the current database revision
	$(load_env) $(ALEMBIC) current

history: ## Show the migration history
	$(load_env) $(ALEMBIC) history

heads: ## Show current migration heads
	$(load_env) $(ALEMBIC) heads

check: ## Fail if models and the database are out of sync
	$(load_env) $(ALEMBIC) check

ruff: ## Lint with ruff (override with args="--fix")
	$(UV) run ruff check . $(args)

format: ## Format Python with ruff
	$(UV) run ruff format .

format-check: ## Fail if ruff would reformat any file
	$(UV) run ruff format --check .

mypy: ## Type-check with mypy
	$(UV) run mypy .

bandit: ## Scan for common security issues
	$(UV) run bandit -c pyproject.toml -r .

lint: ## Run ruff, the formatter check, mypy, and bandit
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(UV) run mypy .
	$(UV) run bandit -c pyproject.toml -r .
