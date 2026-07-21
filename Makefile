PYTHON := .venv/bin/python
PYTHON_FILES := calculator.py create-plan.py local-coder.py review-diff.py \
	run-plan.py runtime/*.py test_*.py

.PHONY: health format format-check lint agent-check agent-install agent-smoke handoff-check test verify \
	metrics context-benchmark plan-check plan-generate plan-candidate-check \
	review review-cached skills runs

health:
	@curl -fsS http://127.0.0.1:8080/health | jq

format:
	$(PYTHON) -m black $(PYTHON_FILES)

format-check:
	$(PYTHON) -m black --check $(PYTHON_FILES)

lint:
	$(PYTHON) -m flake8 -j 1 $(PYTHON_FILES)

agent-check:
	$(PYTHON) -m py_compile $(PYTHON_FILES)
	$(PYTHON) -m json.tool UPSTREAM.json >/dev/null

agent-install:
	$(PYTHON) -m pip install -r requirements-agent.txt

agent-smoke:
	$(PYTHON) -m runtime.smoke

handoff-check: verify agent-smoke
	@test -f AGENTS.md -a -f HANDOFF.md -a -f ARCHITECTURE.md
	@test -z "$$(git status --porcelain)" || (echo "Handoff check failed: working tree is not clean."; git status --short; exit 1)

test:
	$(PYTHON) -m pytest -q --tb=short

context-benchmark:
	$(PYTHON) benchmarks/context_benchmark.py

verify: format-check lint agent-check test
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

review:
	$(PYTHON) review-diff.py

review-cached:
	$(PYTHON) review-diff.py --cached

skills:
	$(PYTHON) local-coder.py skills

runs:
	$(PYTHON) local-coder.py runs
