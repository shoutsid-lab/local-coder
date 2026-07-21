.PHONY: health test lint verify metrics clean-aider

health:
	@curl -fsS http://127.0.0.1:8080/health | jq

test:
	python -m pytest -q

lint:
	python -m flake8 calculator.py test_calculator.py test_pipeline_contract.py

verify: lint test
	git diff --check

metrics:
	@curl -s http://127.0.0.1:8080/metrics | \
	grep -E 'n_tokens_max|prompt_tokens_total|predicted_tokens_total|prompt_tokens_seconds|predicted_tokens_seconds'

clean-aider:
	rm -f aider