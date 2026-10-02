# The following is based on the instructions at: https://docs.github.com/en/code-security/codeql-cli/getting-started-with-the-codeql-cli/setting-up-the-codeql-cli. Follow the instructions at: Setting up the CodeQL CLI

# Prerequisites:
# 1) Install codeql in a tools/ directory that is a peer to the folder containing BenchmarkPython. For example, if you have a git/ folder, which contains BenchmarkPython, BenchmarkUtils, etc., then the tools/ folder would be at the same level as the git/ folder.  i.e., relative to BenchmarkPython, it is at ../tools/codeql-home.
# 2) Then the owasp-benchmark for Python database has to be initialized by running the translateCodeQL.sh script.

# Mac Users: "If you are using macOS on Apple Silicon (for example, Apple M1), ensure that the Xcode command-line developer tools and Rosetta 2 are installed."
## For Xcode command line, run: xcode-select -p 1>/dev/null;echo $?   - If this returns 0, its installed, if 2, its not installed.
## For Rosetta 2, run: lsbom -f /Library/Apple/System/Library/Receipts/com.apple.pkg.RosettaUpdateAuto.bom - And if it returns a list of files, it's installed.

# TODO: Figure out how to get Benchmark version from expectedresults file name to include in results file name

# This then runs the codeql scan:
../tools/codeql-home/codeql/codeql database analyze benchmark-py codeql/python-queries --format=sarifv2.1.0 --output=results/Benchmark-codeql_python-queries.sarif

