#!/usr/bin/env bash
set -euo pipefail

WORKSPACE_DIR="${WORKSPACE_FOLDER:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}"
VENV_DIR="${WORKSPACE_DIR}/.venv"

python3 -m venv "${VENV_DIR}"
"${VENV_DIR}/bin/pip" install --upgrade pip
"${VENV_DIR}/bin/pip" install jupyterlab ipykernel fastapi 'uvicorn[standard]' 'httpx[http2]' pyoxigraph fastembed openpyxl
"${VENV_DIR}/bin/python" -m ipykernel install --user --name jarvet --display-name "Python (jarvet)"
"${WORKSPACE_DIR}/scripts/init-onet-data.sh"
"${VENV_DIR}/bin/python" "${WORKSPACE_DIR}/scripts/init-onet-store.py"
"${VENV_DIR}/bin/python" "${WORKSPACE_DIR}/scripts/init-va-data.py"
"${VENV_DIR}/bin/python" "${WORKSPACE_DIR}/scripts/init-ipeds-data.py"
