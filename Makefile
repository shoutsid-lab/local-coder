PYTHON := .venv/bin/python
FILES := calculator.py test_calculator.py test_pipeline_contract.py

.PHONY: health format format-check lint test verify metrics context-benchmark plan-check plan-generate plan-candidate-check

health:
	@curl -fsS http://127.0.0.1:8080/health | jq

format:
	$(PYTHON) -m black $(FILES)

format-check:
	$(PYTHON) -m black --check $(FILES)

lint:
	$(PYTHON) -m flake8 $(FILES)

test:
	$(PYTHON) -m pytest -q --tb=short

context-benchmark:
	$(PYTHON) benchmarks/context_benchmark.py

verify: format-check lint test
	git diff --check

metrics:
	@curl -s http://127.0.0.1:8080/metrics | \
	grep -E 'n_tokens_max|prompt_tokens_total|predicted_tokens_total|prompt_tokens_seconds|predicted_tokens_seconds'

clean-aider:
	rm -f aider

plan-check:
	$(PYTHON) -m json.tool PLAN.json >/dev/null
	$(PYTHON) run-plan.py --dry-run

plan-generate:
	$(PYTHON) create-plan.py \
		--context calculator.py test_calculator.py

plan-candidate-check:
	$(PYTHON) -m json.tool PLAN.candidate.json >/dev/null
	$(PYTHON) run-plan.py \
		--plan PLAN.candidate.json \
		--dry-run
