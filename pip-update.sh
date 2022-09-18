#!/usr/bin/env sh

set -euo pipefail

pip-compile -U requirements.in --output-file requirements.txt
pip-compile -U requirements.in requirements_dev.in docs/requirements.in --output-file requirements_dev.txt
pip-compile -U docs/requirements.in --output-file docs/requirements.txt
