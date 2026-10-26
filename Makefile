PY ?= .venv/bin/python
UV ?= uv

.PHONY: setup corpus build test lint eval eval-llm api charts powerbi sync docker clean

setup:            ## create venv and install everything (OCR also needs the tesseract binary)
	$(UV) sync --locked --extra llm --extra dev

corpus:           ## generate the synthetic PDLs and their truth
	$(PY) -m pdlwatch.cli corpus

build:            ## extract the corpus, score it, write data/pdlwatch.duckdb + results.json
	$(PY) -m pdlwatch.cli build

test:             ## unit + integration tests (offline, mock LLM)
	$(PY) -m pytest

lint:             ## ruff + format check + mypy
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .
	$(PY) -m mypy

eval:             ## agent eval on golden questions with the offline mock provider
	$(PY) -m pdlwatch.evals.run_evals --provider mock --min-pass-rate 0.95

eval-llm:         ## agent eval with a real model (set provider credentials and PDLWATCH_MODEL)
	$(PY) -m pdlwatch.evals.run_evals --provider $${PDLWATCH_PROVIDER:-anthropic}

api:              ## REST API on :8000
	$(PY) -m uvicorn pdlwatch.api:app --reload --port 8000

charts:           ## README charts
	$(PY) -m pdlwatch.cli charts

powerbi:          ## Power BI star schema CSVs in powerbi/data
	$(PY) -m pdlwatch.cli export-powerbi

sync:             ## download new state PDL versions (sources/medicaid_pdls.yaml) and extract them
	PDLWATCH_DB=data/real/pdlwatch.duckdb $(PY) -m pdlwatch.cli sync

docker:
	docker build -t pdlwatch .

clean:
	rm -rf data reports .pytest_cache .mypy_cache .ruff_cache powerbi/data
