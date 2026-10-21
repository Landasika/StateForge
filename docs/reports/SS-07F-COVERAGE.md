# SS-07F 补充实验：基本块覆盖率

本实验在冻结协议之外,对 held-out seed 5 的各臂重跑 coverage.py,把执行行
映射到 ICFG 基本块,回答"Session 引导是否也带来更高覆盖"。

## 方法

- 工具:coverage.py 7.16.2,`COVERAGE_CORE=sysmon`。
- 分母:应用源码树的全部 ICFG 基本块(块 ID `文件::函数::行span::类型::序号`)。
- 判定:块 span 内任一行被执行即算覆盖。
- 复现校验:每臂重跑的 HTTP 数与冻结 artifacts 逐一对比,全部一致
  (`reproduction_all_match=true`;sequence 19/46/52/46/64,c 8–9,
  d 6,replay 3)。

### 必须记录的方法学坑

1. **settrace 冲突**:引擎的 block-id tracer 用 `sys.settrace`,会静默顶掉
   coverage.py 默认的 settrace core。第一轮数字只测到导入期代码,
   全部作废重跑。改用 PEP 669 `sysmon` core 后两者共存,且引擎侧
   `suffix_target_covered` 依旧为 true。
2. **entry/exit 块导入即覆盖**:挂在 def 行上的块在模块导入时就会
   "被覆盖"。目标函数命中判定只认 span 起始行**严格大于 def 行**的体块。
3. **SINGLE 是近似**:Atheris 的 trace 钩子与 coverage.py 冲突,SINGLE
   用同请求池、同 seed 的 40 个随机单请求复现,非 Atheris 原始序列。

## 结果(seed 5,`*`=目标函数体块被执行)

```text
应用          总块数   SINGLE      SEQUENCE    C           D           replay
microblog       579   37.13%      41.45% *    41.28% *    40.59% *    40.59% *
CTFd           7784   31.05%      32.23% *    32.04% *    31.33% *    31.33% *
healthchecks   6637   23.10%      23.72%      23.43% *    23.37% *    23.37% *
linkding       4277   24.78%      29.83% *    29.60% *    29.39% *    29.39% *
Spirit         4695   27.75%      27.88%      28.58% *    27.03% *    27.03% *
```

对应的 HTTP 请求数:

```text
应用          SINGLE   SEQUENCE    C     D     replay
microblog        40        19       9     6       3
CTFd             40        46       8     6       3
healthchecks     40        52       8     6       3
linkding         40        46       8     6       3
Spirit           40        64       8     6       3
```

## 读法

1. **外部工具证实了命中结论**:coverage 测出的目标函数体块命中
   (SINGLE 0/5,SEQUENCE 3/5:mb/ctfd/ld,C/D/replay 5/5)与冻结
   artifacts 的命中记录逐点一致——引擎自报的"目标块已执行"有独立
   测量佐证。
2. **C 用 8 次请求达到或超过 SINGLE 40 次请求的整体覆盖**:五个应用
   全部如此(microblog +4.15pp、linkding +4.82pp、CTFd +0.99pp、
   healthchecks +0.33pp、Spirit +0.83pp),并且进入 SINGLE 进不去的
   登录后代码。
3. **SEQUENCE 花最多请求(46–64)买到最高整体覆盖,但买不到受保护代码**:
   healthchecks/Spirit 上它覆盖率略高于或接近 C,目标函数体块却没执行。
   Spirit 上 C 以 8 次请求覆盖 28.58%,高于 SEQUENCE 64 次请求的 27.88%,
   且进了目标体。
4. **绝对百分比被导入期代码稀释**:Django/Flask 应用启动本身执行大量
   模块级代码,所以差异看起来只有几个百分点;受保护代码的可达性信号
   是体块命中,不是整体百分比。

## 限制

- 冻结协议之外的补充实验,不回写进 SS-07F 冻结清单;
- SINGLE 为请求池随机近似(见上);
- 覆盖率只统计应用源码树内 ICFG 可解析的 Python 文件,不含模板/静态;
- 仅 seed 5 一个点。

## 工件

```text
stateforge/evaluation/real_app_coverage.py
artifacts/session_ss07f/coverage_supp/<app>/<arm>/coverage.json
artifacts/session_ss07f/coverage_supp/COVERAGE_SUMMARY.json
```

## 追加:启动地板与目标文件分解

"为什么总覆盖率只涨几个点"的答案在分母结构:

```text
应用          boot地板(0请求)   SINGLE增量     C增量
microblog        34.02%        +3.11pp      +7.26pp
CTFd             26.34%        +4.71pp      +5.70pp
healthchecks     10.89%       +12.21pp     +12.54pp
linkding          9.40%       +15.38pp     +20.20pp
Spirit            7.56%       +20.19pp     +21.02pp
```

光 import 应用就执行了 9%–34% 的块(microblog 197/579)。任务只做
"登录一次+访问一个受保护页",新增执行代码注定只有几十个块,总百分比
被启动代码稀释。

目标文件(受 Session 保护的 views 层)内部:

```text
应用          目标文件                 SINGLE    C
microblog     main/routes.py            33/104   38/104
CTFd          challenges.py              2/30    24/30
healthchecks  accounts/views.py         98/541  106/541
linkding      views/bookmarks.py        52/331   64/331
Spirit        user/views.py             20/83    27/83
```

CTFd 最典型:SINGLE 40 个请求在 challenges.py 只碰 2 个块(def 行),
C 8 个请求碰 24/30。结论:Session 引导买到的是受保护代码的**可达性**,
不是整体覆盖的铺开;总覆盖率不是这件事的正确尺子。

## 追加:未覆盖块到底是什么(replay 臂)

按函数级拆解,未覆盖块的主导构成是**整个函数从未被调用**,不是分支没走到:

```text
应用          未覆盖块   从未调用的函数体块(占比)   调用过但分支未走
microblog       344      313 (91%)                  22
CTFd           5345      4234 (79%)                317
healthchecks   5086      4528 (89%)                 76
linkding       3020      2474 (82%)                 94
Spirit         3426      2128 (62%)                 70
```

"从未调用"的都是其它真实端点,例如:

```text
microblog routes.py 14 个函数只有 2 个被调用(index/before_request);
                  follow/unfollow/edit_profile/search/messages 全没碰
CTFd               register(93块) api/v1/challenges.py::post(104块)
                  teams::new(88) files(81) import_ctf(167)
healthchecks       accounts::project(103) api::ping(60) front::checks(58)
Spirit            comment::publish(25) topic::publish(18) like::create(17)
```

结构性不可达部分:management 命令(healthchecks 468 块/linkding 84/
Spirit 41,web 运行永不触发,0 覆盖)、Spirit 源码树内 tests 1539 块
(占其分母 33%)、admin 后台部分。

"差一点"的典型:index() 里未走的 4 块恰好是 POST 发帖分支
(32/34/35-39/40 行,detect→建 Post→commit→redirect)——实验只
GET 读过 /index,从未 POST 写。未覆盖主因是任务只请求了 2–3 个
端点,不是引擎进不去。
