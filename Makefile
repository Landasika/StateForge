# StateForge 本地 CI 入口(依赖唯一来源:pyproject.toml)
#   make install  安装 editable + dev/test/fuzz extras
#   make test     全量 pytest(开发树口径)
#   make demo     SF-Micro 微基准(21 用例全 1.00)
#   make verify   ./ci.sh 完整 fresh-clone 验收链(clone→venv→install→pytest→demo)
#   make artifact sha256 完整性清单(R10:仓库即 artifact,无 tarball;
#                 git ls-files 只列 tracked 文件=排除规则,anota/external/results
#                 从不入清单;符号链接按链接文本哈希(git 对 symlink 存的就是
#                 链接文本,直接 sha256sum 会顺着目录链接炸 "Is a directory");
#                 artifact-manifest.sha256 为构建产物,不入库)
#   make clean    清理缓存/构建产物(仅仓库内,不触碰 external/ anota/ .git)

.PHONY: install test demo verify artifact clean

PYTHON ?= python3

install:
	$(PYTHON) -m pip install -e ".[dev,test,fuzz]"

test:
	$(PYTHON) -m pytest

demo:
	$(PYTHON) -m stateforge.evaluation.micro_benchmark

verify:
	./ci.sh

artifact:
	git ls-files | while IFS= read -r f; do \
		if [ -L "$$f" ]; then \
			printf '%s  %s\n' "$$(printf %s "$$(readlink "$$f")" \
				| sha256sum | cut -d' ' -f1)" "$$f"; \
		else \
			sha256sum "$$f"; \
		fi; \
	done > artifact-manifest.sha256

clean:
	find . \( -name .git -o -name external -o -name anota \) -prune \
		-o -type d \( -name __pycache__ -o -name .pytest_cache -o -name '*.egg-info' \) \
		-exec rm -rf {} +
	rm -rf results/micro
	rm -f artifact-manifest.sha256
