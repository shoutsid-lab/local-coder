PYTHON := .venv/bin/python
PYTHON_FILES := local-coder.py review-diff.py run-editor.py evaluation/*.py runtime/*.py runtime/dspy_programs/*.py tests/*.py

.PHONY: health format format-check lint agent-check agent-install agent-smoke handoff-check test verify \
	metrics review review-cached skills skills-lint gepa-dataset-check gepa-runner-check runs live-e2e live-e2e-report

health:
	@curl -fsS http://127.0.0.1:8080/health | jq

format:
	@for file in $(PYTHON_FILES); do \
		$(PYTHON) -m black --quiet "$$file" || exit 1; \
	done

format-check:
	@for file in $(PYTHON_FILES); do \
		$(PYTHON) -m black --check --quiet "$$file" || exit 1; \
	done

lint:
	$(PYTHON) -m flake8 -j 1 $(PYTHON_FILES)

agent-check:
	$(PYTHON) -m py_compile $(PYTHON_FILES)
	$(PYTHON) -m json.tool docs/UPSTREAM.json >/dev/null
	$(PYTHON) -m json.tool evaluation/suites/atomic-v1.json >/dev/null

agent-install:
	$(PYTHON) -m pip install -r requirements-agent.txt

agent-smoke:
	$(PYTHON) -m runtime.smoke

handoff-check: verify agent-smoke
	@test -f AGENTS.md -a -f ROADMAP.md -a -f docs/HANDOFF.md \
		-a -f docs/ARCHITECTURE.md \
		-a -f docs/PIPELINE.md -a -f docs/CONVENTIONS.md \
		-a -f docs/RECURSIVE_IMPROVEMENT.md
	@test -z "$$(git status --porcelain)" || (echo "Handoff check failed: working tree is not clean."; git status --short; exit 1)

test:
	$(PYTHON) -m pytest -q --tb=short

verify: format-check lint agent-check test
	git diff --check

metrics:
	@curl -s http://127.0.0.1:8080/metrics | \
	grep -E 'n_tokens_max|prompt_tokens_total|predicted_tokens_total|prompt_tokens_seconds|predicted_tokens_seconds'

review:
	@test -n "$(TASK)" || (echo "Usage: make review TASK=path/to/task.md"; exit 1)
	$(PYTHON) review-diff.py --task "$(TASK)"

review-cached:
	@test -n "$(TASK)" || (echo "Usage: make review-cached TASK=path/to/task.md"; exit 1)
	$(PYTHON) review-diff.py --cached --task "$(TASK)"

skills:
	$(PYTHON) local-coder.py skills

skills-lint:
	$(PYTHON) -m runtime.skills_lint .local-coder/skills

gepa-dataset-check:
	$(PYTHON) -m pytest -q --tb=short tests/test_gepa_dataset.py

gepa-runner-check:
	$(PYTHON) -m pytest -q --tb=short tests/test_verification_evidence.py tests/test_gepa_runner.py

runs:
	$(PYTHON) local-coder.py runs

live-e2e:
	@rm -f .local-coder/live-e2e/latest-summary.json
	$(MAKE) skills-lint verify agent-smoke
	$(PYTHON) -m runtime.live_e2e

live-e2e-report:
	$(PYTHON) -m runtime.live_e2e_report
