UV ?= uv

.PHONY: install install-pip test evals demo harness smoke serve lint

install:
	$(UV) sync --extra dev

# pip fallback is two-step: build the wheel environment from the
# frozen constraints, then install the project against them.
install-pip:
	python -m pip install -r constraints.txt
	python -m pip install --no-deps -e .

test:
	$(UV) run pytest -q

evals:
	$(UV) run python evals/run_evals.py

demo:
	$(UV) run python scripts/demo.py

harness:
	$(UV) run python scripts/local_harness.py

smoke:
	$(UV) run python scripts/prod_smoke.py

serve:
	$(UV) run python -m returns_resolution.runtime_app
