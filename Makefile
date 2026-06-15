.PHONY: install reinstall clean lint typecheck test

install:
	uv pip install -e .

reinstall:
	uv tool install -e . --force

clean:
	rm -rf .venv
	uv venv

lint:
	cd ap-web && npm run lint

typecheck:
	cd ap-web && npm run type-check

test:
	cd ap-web && npm test
