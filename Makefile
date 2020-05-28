travis-install:
	pip install -r docs/requirements.txt
	pip install -r tests/requirements.txt
	pip install -r requirements_dev.txt
	openssl aes-256-cbc -d -md rmd160 -pass pass:$(OPENSSL_PASS) -in tests/creds.json.enc -out tests/creds.json
	cp tests/.env.example tests/.env

version-tag:
	git describe --tags > version_tag

wheel: version-tag
	python setup.py sdist bdist_wheel

travis-script: wheel
	pip install dist/gspread_asyncio*.whl
	cd docs && $(MAKE) html

# I use this locally to encrypt the creds.json
encrypt-test-files:
	openssl aes-256-cbc -md rmd160 -pass pass:$(OPENSSL_PASS) -in tests/creds.json -out tests/creds.json.enc

# You can use this to run the tests locally
test:
	. tests/.env && python -m unittest tests.integration

BLACK_FILES=*.py gspread_asyncio/*.py tests/*.py docs/*.py
format:
	black $(BLACK_FILES)

format-check:
	black --check $(BLACK_FILES)
