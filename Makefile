PYTHON := .venv/bin/python
FILES := calculator.py test_calculator.py test_pipeline_contract.py

.PHONY: health format format-check lint test verify metrics clean-aider

health:
	@curl -fsS http://127.0.0.1:8080/health | jq

format:
	$(PYTHON) -m black $(FILES)

format-check:
	$(PYTHON) -m black --check $(FILES)

lint:
	$(PYTHON) -m flake8 $(FILES)

test:
	$(PYTHON) -m pytest -q

verify: format-check lint test
	git diff --check

metrics:
	@curl -s http://127.0.0.1:8080/metrics | \
	grep -E 'n_tokens_max|prompt_tokens_total|predicted_tokens_total|prompt_tokens_seconds|predicted_tokens_seconds'

clean-aider:
	rm -f aider
