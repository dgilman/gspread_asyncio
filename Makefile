travis-install:
	pip install -r requirements_dev.txt
	cp tests/.env.example tests/.env

black-install:
	pip install -r requirements_dev.txt

version-tag:
	git describe --tags > version_tag

docs:
	cd docs && $(MAKE) html

# I use this locally to encrypt the creds.json
encrypt-test-files:
	age -r age1ajzzxt7ey6gxkfjcynw2tnwd95gggcz7t5jmt2pr8jga54tl39rsdxy9fn -o tests/creds.json.age tests/creds.json

decrypt-test-files:
	echo "$(AGE_KEY)" > tests/age_key.txt
	./age/age --decrypt -i tests/age_key.txt -o tests/creds.json tests/creds.json.age

# You can use this to run the tests locally
test:
	. tests/.env && python -m unittest tests.integration

BLACK_FILES=*.py gspread_asyncio/*.py tests/*.py docs/*.py
format:
	black $(BLACK_FILES)
	isort $(BLACK_FILES)

format-check:
	black --check $(BLACK_FILES)
	isort --check $(BLACK_FILES)
