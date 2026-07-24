PYTHON := .venv/bin/python
PYTHON_FILES := local-coder.py review-diff.py run-editor.py evaluation/*.py runtime/*.py runtime/dspy_programs/*.py tests/*.py

.PHONY: health format format-check lint agent-check agent-install agent-smoke handoff-check test verify \
	metrics review review-cached skills skills-lint gepa-dataset-check gepa-runner-check \
	gepa-experiment-check prompt-campaign-check prompt-deployment-check route-probe-check \
	route-profile-check route-qualification-check route-qualification-collect-check \
	route-contract-diagnostic-check route-contract-diagnostic-collect \
	route-contract-diagnostic-compare route-adapter-diagnostic-check \
	route-adapter-diagnostic-collect route-adapter-diagnostic-compare \
	real-task-corpus-check real-task-corpus-summary real-task-development-check \
	real-task-development-collect real-task-profile-tuning-check \
	real-task-profile-tuning-collect real-task-profile-tuning-compare \
	real-task-prompt-tuning-check real-task-prompt-tuning-collect \
	real-task-prompt-tuning-compare route-qualification-policy-hash \
	route-qualification-collect route-qualification route-probe runs live-e2e \
	live-e2e-report

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
	$(PYTHON) -m json.tool evaluation/suites/atomic-v1.json >/dev/null
	$(PYTHON) -m json.tool profiles/qwythos-f3-qualification-v1.json >/dev/null
	$(PYTHON) -m json.tool profiles/qwythos-f3-focused-contract-v2.json >/dev/null
	$(PYTHON) -m json.tool profiles/qwythos-f3-adapter-contract-v1.json >/dev/null
	$(PYTHON) -m json.tool evaluation/real_task_cases/development-v1.json >/dev/null
	$(PYTHON) -m json.tool evaluation/real_task_cases/holdout-v1.index.json >/dev/null
	$(PYTHON) -m json.tool profiles/track-g-development-v1.json >/dev/null
	$(PYTHON) -m json.tool profiles/track-g-qwythos-tuning-v1.json >/dev/null
	$(PYTHON) -m json.tool profiles/track-g-qwythos-prompt-tuning-v1.json >/dev/null

agent-install:
	$(PYTHON) -m pip install -r requirements-agent.txt

agent-smoke:
	$(PYTHON) -m runtime.smoke

handoff-check: verify agent-smoke
	@test -f AGENTS.md -a -f ROADMAP.md -a -f docs/HANDOFF.md \
		-a -f docs/ARCHITECTURE.md \
		-a -f docs/PIPELINE.md -a -f docs/CONVENTIONS.md \
		-a -f docs/RECURSIVE_IMPROVEMENT.md \
		-a -f docs/PROMPT_DEPLOYMENT.md
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
	$(PYTHON) -m pytest -q --tb=short tests/test_verification_evidence.py tests/test_gepa_runner.py tests/test_gepa_optimization_hygiene.py

gepa-experiment-check:
	$(PYTHON) -m pytest -q --tb=short tests/test_gepa_experiment.py

prompt-campaign-check:
	$(PYTHON) -m pytest -q --tb=short \
		tests/test_prompt_campaign.py tests/test_prompt_evaluation.py

prompt-deployment-check:
	$(PYTHON) -m pytest -q --tb=short tests/test_prompt_activation.py

route-probe-check:
	$(PYTHON) -m pytest -q --tb=short tests/test_model_response.py

route-profile-check:
	$(PYTHON) -m pytest -q --tb=short tests/test_route_profiles.py

route-qualification-check:
	$(PYTHON) -m pytest -q --tb=short tests/test_route_qualification.py

route-qualification-collect-check:
	$(PYTHON) -m pytest -q --tb=short tests/test_route_qualification_collect.py

route-contract-diagnostic-check:
	$(PYTHON) -m pytest -q --tb=short tests/test_route_contract_diagnostic.py

route-contract-diagnostic-collect:
	@test -n "$(SUBJECT)" -a -n "$(ENVIRONMENT)" || (echo "Usage: make route-contract-diagnostic-collect SUBJECT=baseline|candidate ENVIRONMENT=machine-id [OUTPUT=path]"; exit 1)
	$(PYTHON) -m runtime.route_contract_diagnostic collect \
		--subject "$(SUBJECT)" \
		--environment-id "$(ENVIRONMENT)" \
		$(if $(OUTPUT),--output "$(OUTPUT)",)

route-contract-diagnostic-compare:
	@test -n "$(BASELINE)" -a -n "$(CANDIDATE)" || (echo "Usage: make route-contract-diagnostic-compare BASELINE=baseline.json CANDIDATE=candidate.json [OUTPUT=path]"; exit 1)
	$(PYTHON) -m runtime.route_contract_diagnostic compare \
		--baseline "$(BASELINE)" \
		--candidate "$(CANDIDATE)" \
		$(if $(OUTPUT),--output "$(OUTPUT)",)

route-adapter-diagnostic-check:
	$(PYTHON) -m pytest -q --tb=short tests/test_route_adapter_diagnostic.py

route-adapter-diagnostic-collect:
	@test -n "$(SUBJECT)" -a -n "$(ENVIRONMENT)" || (echo "Usage: make route-adapter-diagnostic-collect SUBJECT=baseline|candidate ENVIRONMENT=machine-id [OUTPUT=path]"; exit 1)
	$(PYTHON) -m runtime.route_adapter_diagnostic collect \
		--subject "$(SUBJECT)" \
		--environment-id "$(ENVIRONMENT)" \
		$(if $(OUTPUT),--output "$(OUTPUT)",)

route-adapter-diagnostic-compare:
	@test -n "$(BASELINE)" -a -n "$(CANDIDATE)" || (echo "Usage: make route-adapter-diagnostic-compare BASELINE=baseline.json CANDIDATE=candidate.json [OUTPUT=path]"; exit 1)
	$(PYTHON) -m runtime.route_adapter_diagnostic compare \
		--baseline "$(BASELINE)" \
		--candidate "$(CANDIDATE)" \
		$(if $(OUTPUT),--output "$(OUTPUT)",)

real-task-corpus-check:
	$(PYTHON) -m pytest -q --tb=short tests/test_real_task_corpus.py
	$(PYTHON) -m evaluation.real_task_corpus \
		$(if $(HOLDOUT),--holdout-suite "$(HOLDOUT)",)

real-task-corpus-summary:
	$(PYTHON) -m evaluation.real_task_corpus

real-task-development-check:
	$(PYTHON) -m pytest -q --tb=short tests/test_real_task_development.py

real-task-development-collect:
	@test -n "$(SUBJECT)" -a -n "$(ENVIRONMENT)" || (echo "Usage: make real-task-development-collect SUBJECT=baseline|candidate ENVIRONMENT=machine-id [OUTPUT=path]"; exit 1)
	$(PYTHON) -m evaluation.real_task_development \
		--subject "$(SUBJECT)" \
		--environment-id "$(ENVIRONMENT)" \
		$(if $(OUTPUT),--output "$(OUTPUT)",)

real-task-profile-tuning-check:
	$(PYTHON) -m pytest -q --tb=short tests/test_real_task_profile_tuning.py

real-task-profile-tuning-collect:
	@test -n "$(PROFILE)" -a -n "$(ENVIRONMENT)" || (echo "Usage: make real-task-profile-tuning-collect PROFILE=current-control|deterministic-accuracy|role-depth-accuracy ENVIRONMENT=machine-id [OUTPUT=path]"; exit 1)
	$(PYTHON) -m evaluation.real_task_profile_tuning collect \
		--profile "$(PROFILE)" \
		--environment-id "$(ENVIRONMENT)" \
		$(if $(OUTPUT),--output "$(OUTPUT)",)

real-task-profile-tuning-compare:
	@test -n "$(REPORTS)" || (echo "Usage: make real-task-profile-tuning-compare REPORTS='path1 path2 path3' [OUTPUT=path]"; exit 1)
	$(PYTHON) -m evaluation.real_task_profile_tuning compare \
		--reports $(REPORTS) \
		$(if $(OUTPUT),--output "$(OUTPUT)",)

real-task-prompt-tuning-check:
	$(PYTHON) -m pytest -q --tb=short tests/test_real_task_prompt_tuning.py

real-task-prompt-tuning-collect:
	@test -n "$(PROMPT_PROFILE)" -a -n "$(ENVIRONMENT)" || (echo "Usage: make real-task-prompt-tuning-collect PROMPT_PROFILE=code-control|evidence-completeness|field-checklist ENVIRONMENT=machine-id [OUTPUT=path]"; exit 1)
	$(PYTHON) -m evaluation.real_task_prompt_tuning collect \
		--prompt-profile "$(PROMPT_PROFILE)" \
		--environment-id "$(ENVIRONMENT)" \
		$(if $(OUTPUT),--output "$(OUTPUT)",)

real-task-prompt-tuning-compare:
	@test -n "$(REPORTS)" || (echo "Usage: make real-task-prompt-tuning-compare REPORTS='path1 path2 path3' [OUTPUT=path]"; exit 1)
	$(PYTHON) -m evaluation.real_task_prompt_tuning compare \
		--reports $(REPORTS) \
		$(if $(OUTPUT),--output "$(OUTPUT)",)

route-qualification-policy-hash:
	$(PYTHON) -m runtime.route_qualification --print-policy-hash

route-qualification-collect:
	@test -n "$(ENVIRONMENT)" || (echo "Usage: make route-qualification-collect ENVIRONMENT=machine-id [OUTPUT=path] [SERVER_PID=pid] [STARTUP_SECONDS=n] [MODEL_SWITCH_SECONDS=n] [PEAK_VRAM_MIB=n]"; exit 1)
	$(PYTHON) -m runtime.route_qualification_collect \
		--environment-id "$(ENVIRONMENT)" \
		$(if $(OUTPUT),--output "$(OUTPUT)",) \
		$(if $(SERVER_PID),--server-pid "$(SERVER_PID)",) \
		$(if $(STARTUP_SECONDS),--startup-seconds "$(STARTUP_SECONDS)",) \
		$(if $(MODEL_SWITCH_SECONDS),--model-switch-seconds "$(MODEL_SWITCH_SECONDS)",) \
		$(if $(PEAK_VRAM_MIB),--peak-vram-mib "$(PEAK_VRAM_MIB)",)

route-qualification:
	@test -n "$(EVIDENCE)" || (echo "Usage: make route-qualification EVIDENCE=path/to/report.json [REQUIRE=any|planner|reviewer|both]"; exit 1)
	$(PYTHON) -m runtime.route_qualification "$(EVIDENCE)" $(if $(REQUIRE),--require "$(REQUIRE)",)

route-probe:
	@test -n "$(ROUTE)" || (echo "Usage: make route-probe ROUTE=local-fast [MODE=exact|reasoning]"; exit 1)
	$(PYTHON) -m runtime.route_probe --route "$(ROUTE)" --mode "$(if $(MODE),$(MODE),exact)"

runs:
	$(PYTHON) local-coder.py runs

live-e2e:
	@rm -f .local-coder/live-e2e/latest-summary.json
	$(MAKE) skills-lint verify agent-smoke
	$(PYTHON) -m runtime.live_e2e

live-e2e-report:
	$(PYTHON) -m runtime.live_e2e_report
