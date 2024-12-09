check: format lint test clean

SOURCE_FILES=mongospecs tests

install:
	pip install -e .

test:
	pytest -vv --capture=tee-sys

clean:
	rm -rf build/ dist/ *.egg-info .*_cache
	find . -name '*.pyc' -type f -exec rm -rf {} +
	find . -name '__pycache__' -exec rm -rf {} +

package: clean
	python -m build

publish: package
	twine upload dist/*

format:
	ruff format ${SOURCE_FILES}
	ruff check ${SOURCE_FILES} --fix

lint:
	ruff format --check --diff ${SOURCE_FILES}
	ruff check ${SOURCE_FILES}
	mypy mongospecs --strict
	mypy tests

shell:
	source .venv/bin/activate

.PHONY: test clean