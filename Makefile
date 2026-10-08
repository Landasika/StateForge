# StateForge 本地 CI 入口(依赖唯一来源:pyproject.toml)
#   make install  安装 editable + dev/test/fuzz extras
#   make test     全量 pytest(开发树口径)
#   make demo     SF-Micro 微基准(21 用例全 1.00)
#   make verify   ./ci.sh 完整 fresh-clone 验收链(clone→venv→install→pytest→demo)
#   make clean    清理缓存/构建产物(仅仓库内,不触碰 external/ anota/ .git)

.PHONY: install test demo verify clean

PYTHON ?= python3

install:
	$(PYTHON) -m pip install -e ".[dev,test,fuzz]"

test:
	$(PYTHON) -m pytest

demo:
	$(PYTHON) -m stateforge.evaluation.micro_benchmark

verify:
	./ci.sh

clean:
	find . \( -name .git -o -name external -o -name anota \) -prune \
		-o -type d \( -name __pycache__ -o -name .pytest_cache -o -name '*.egg-info' \) \
		-exec rm -rf {} +
	rm -rf results/micro
