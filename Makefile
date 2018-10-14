travis-install:
	pip install -r docs/requirements.txt

version-tag:
	git describe --tags > version_tag

wheel: version-tag
	python setup.py sdist bdist_wheel

travis-script: wheel
	pip install dist/gspread_asyncio*.whl
	cd docs && $(MAKE) html
