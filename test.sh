#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/venv"
REQUIREMENTS="$SCRIPT_DIR/requirements.txt"

# Check if venv exists, create if not
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment in $VENV_DIR..."
    python3 -m venv "$VENV_DIR"
fi

# Activate virtual environment
source "$VENV_DIR/bin/activate"

# Install requirements
echo "Installing requirements from $REQUIREMENTS..."
pip install -r "$REQUIREMENTS" --quiet

# Install pytest if not already installed
pip install pytest --quiet 2>/dev/null || true

echo "Running tests..."

mkdir -p "$SCRIPT_DIR/test-reports"

pytest "$SCRIPT_DIR/tests" \
    -v \
    --junitxml="$SCRIPT_DIR/test-reports/results.xml" \
    "$@"

deactivate
