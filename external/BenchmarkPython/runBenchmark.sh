#!/bin/sh
set -e

# ensure you are in the right directory to be running this script
if [ $(basename $(pwd)) != $(basename $(git rev-parse --show-toplevel)) ]; then
	echo "Run this from the root directory of the Benchmark for Python."
	exit 1
fi

# ensure venv exists
if [ ! -d venv ]; then
	echo "Creating python venv..."
	python3 -m venv venv
fi


if [ -n ${VIRTUAL_ENV} ]; then
source venv/bin/activate
ENTERED_VENV=y
fi

pip install -q -r requirements.txt

# TODO: Replace with a real WSGI server (whatever that means)
flask --app app.py run --debug --port 8443 --cert=adhoc


if [ -v ENTERED_VENV ]; then
	deactivate
fi

