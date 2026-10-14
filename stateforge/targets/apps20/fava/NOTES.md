# fava 状态语义笔记(门 4)

应用:beancount 账本 Web UI(fava 1.30.16,PyPI,Flask + flask-babel)。
源码克隆在 external/fava(供静态分析);运行时用 external/fava/.venv 里的
site-packages fava。批 1。

## 跨请求守卫实例(≥2)

### G1 FILE 域:账本文件是全局状态(bfile slug 门)
- 位置:`fava/application.py` — `_LedgerSlugLoader` + `url_value_preprocessor
  _pull_beancount_file`(`g.ledger = ledgers[g.beancount_file_slug]`,KeyError
  → 404)
- 谓词:`EXISTS ledger[<bfile-slug>]`(slug = slugify(option "title"))
- 生产者:进程启动参数 `create_app(files)`;账本 option "title" 决定 slug
- 单请求 fuzz 不可达:不带合法 slug 前缀的一切深路由(所有报告/editor/
  query)直接 404;harness 用 `LEDGERS.first_slug()` 运行时求 slug
- 状态迁移:`/<slug>/editor/`、`/<slug>/api/add_entries`、`/api/format_source`
  写回 .beancount 文件 → `ledger.changed()` 按 mtime 检测 → 整账本重载,
  后续所有请求看到新条目

### G2 URL 域:account/filter/time 全局过滤器
- 位置:`fava/core/__init__.py::FilterEntries`(`g.filtered`,account/
  filter/time 三参);`/jump` 视图按 Referer 重建 query 并替换参数
- 谓词:报告页 query 里携带 `account=<A>&filter=<F>&time=<T>`
- 生产者:`/jump?account=Assets:Cash`(带 Referer)→ 302 到改写后的报告
  URL;报告页内所有链接再传播该状态
- 单请求 fuzz 不可达:深报告(income_statement/journal 等)的"有数据"分支
  依赖过滤器与账本条目相交;坏 filter 串(AdvancedFilter 语法)直接 500

### G3 FILE 域:document/statement 路径守卫
- 位置:`fava/application.py::document` — `is_document_or_import_file(
  filename, g.ledger)`(filename 必须落在账本 documents 目录集合内)
- 谓词:`filename ∈ ledger.options["documents"]` 下的已声明文档
- 生产者:账本里 `filename` metadata / documents 目录条目
- 单请求不可伪造:任意路径 → 404(冒烟里 `?filename=nope` 即此分支)

### 附:/jump 的头域守卫
- `urlparse(request.referrer)`:无 Referer 头时 referrer 为 None →
  urlunparse TypeError → 500(真实行为,冒烟必须带 Referer 头)

## 状态域覆盖
- FILE:G1(账本加载/重载)/ G3(文档路径)
- URL:G2(query 过滤器状态)+ /jump 的 Referer 头依赖
- SESSION:无(fava 默认无登录;incognito/read_only 为启动旗标)

## 接入门记录(2026-10-14)
- 门 1:30/30 non-5xx(冒烟表 harness.smoke();/、/<slug>/、/jump 为 302,
  context/document 为守卫 404)
- 门 2:账本写 tempfile.mkdtemp(含 pid)目录,双进程并行 30/30+30/30,
  仓库零污染 ✓
- 门 3:build_icfg 639 fn/3880 blocks/3852 edges;analyze(flask)
  15 ep/7 cand,1s,venv 零泄漏(分析 external/fava 源码克隆)
- venv:external/fava/.venv(anaconda py3.13 基座;PyPI fava+beancount
  3.2.3 齐备,无需补装)
- 入口:`fava.application.create_app([账本路径], load=True)`;PyPI 包无
  `app.run` 依赖,Babel 译文随包分发,无额外语言文件坑
