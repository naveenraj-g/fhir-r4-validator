# FHIR R4 Validator — Task Runner Recipes

# Default recipe: print available recipes
default:
    @just --list

# ── Development & Server ────────────────────────────────────────────────────────

# Run development server with auto-reload
dev PORT="4567":
    uv run uvicorn app.main:app --host 0.0.0.0 --port {{PORT}} --reload

# Start production server (WORKERS=1 by default: every worker holds its own copy of the definitions in RAM)
start PORT="4567" WORKERS="1":
    uv run uvicorn app.main:app --host 0.0.0.0 --port {{PORT}} --workers {{WORKERS}}

# ── Dependencies & Sync ─────────────────────────────────────────────────────────

# Install / sync all dependencies using uv
install:
    uv sync

# Update lockfile
lock:
    uv lock

# ── Tests ───────────────────────────────────────────────────────────────────────

# Run all tests with verbose output
test:
    uv run pytest tests/ -v

# Run all tests with quiet output
test-q:
    uv run pytest tests/ -q

# Run specific tests by keyword or name
# Usage: just test-k validate
test-k K:
    uv run pytest tests/ -k "{{K}}" -v

# ── Code Quality ────────────────────────────────────────────────────────────────

# Check code formatting and linting
lint:
    uv run ruff check app tests

# Auto-fix lint issues and format code
fmt:
    uv run ruff check --fix app tests
    uv run ruff format app tests

# ── Docker ──────────────────────────────────────────────────────────────────────

# Build Docker image
docker-build TAG="fhir-r4-validator:latest":
    docker build -t {{TAG}} .

# Run Docker container
docker-run PORT="4567" TAG="fhir-r4-validator:latest":
    docker run -p {{PORT}}:4567 {{TAG}}
