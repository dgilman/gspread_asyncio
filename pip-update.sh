#!/usr/bin/env sh

set -euo pipefail

pip-compile requirements.in --output-file requirements.txt
pip-compile requirements.in requirements_dev.in docs/requirements.in --output-file requirements_dev.txt
pip-compile docs/requirements.in --output-file docs/requirements.txt
