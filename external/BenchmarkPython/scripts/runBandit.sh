set -e

# ensure right directory to be running this script
if [ $(basename $(pwd)) != $(basename $(git rev-parse --show-toplevel)) ]; then
	echo "Run this from the root directory of the Benchmark."
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

# TODO: add this file
pip install bandit[sarif] -q

# TODO: Figure out how to get Benchmark version from expectedresults file name to include in results file name
bandit -x "./venv" -f sarif -r . -o results/BenchmarkPython-Bandit.sarif

if [ -v ENTERED_VENV ]; then
	deactivate
fi

