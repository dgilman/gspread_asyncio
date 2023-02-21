#!/usr/bin/env sh

set -euo pipefail

pip-compile --resolver=backtracking -U requirements.in --output-file requirements.txt
pip-compile --resolver=backtracking -U requirements.in requirements_dev.in docs/requirements.in --output-file requirements_dev.txt
pip-compile --resolver=backtracking -U requirements.in requirements_test.in --output-file requirements_test.txt
pip-compile --resolver=backtracking -U requirements_format.in --output-file requirements_format.txt
pip-compile --resolver=backtracking -U docs/requirements.in --output-file docs/requirements.txt
