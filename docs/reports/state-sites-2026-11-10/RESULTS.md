# 状态改变点/守卫点:零词典提取器(2026-11-10)

## 设计原则

**不用框架词典。** 一套语言形状规则,作用域 = 应用 + venv 中可达的框架切片:

1. store 写:`x[k] = v` / `del x[k]` / `pop` / `setdefault` / `update` / `clear` / `flush`
   - 资格:链出现在 ≥2 个函数(跨函数共享),且链头是**入口函数形参**
     (`request.session` ← 框架以参数流入视图)或**模块级导入名**
     (flask 的 `session` 代理)。这排除 `kwargs`/`context` 等
     请求内局部字典——不靠名字,靠"身份是否可能活得比一次请求久"。
2. 守卫:`if <cond>: return/raise`(提前退出形状),记极性与根符号。
3. key 解析:同文件常量折叠;解不出显式标 `<dyn>`/`<name:..>`,绝不猜。
4. 原子不透明:slot 就是 `链["key"]`,不产 "auth" 之类语义名。
5. **三类概念分开(11-10 修正,原文第 4 条后半句作废)**:
   - **状态转移点** = 那条语句本身(`session["id"] = user.id` 执行即
     改持久状态),不锚定"两端"、不配守卫;
   - **守卫** = 读侧另一类事实("这段代码需要什么状态才能过"),
     与"谁改变了状态"是两类事实,规划时 join 使用;
   - **转移记录(边)** = 动态层在转移点前后拍快照的差(from→to);
     静态层只给点,不给边。
   原文"因果边(写点⇒守卫翻转)留给动态确认"的说法**错误**——
   那把转移和守卫混成了一个概念;动态确认的对象是转移点前后的
   快照差,不是"守卫翻转"。

框架不是黑盒:它就是 venv 里的 Python,同一规则可扫。
多根重叠去重(app 根 ⊇ 框架切片时文件只扫一次)。

## 测量(应用 + 框架切片联合扫描,测试目录排除)

| | healthchecks (Django) | CTFd (Flask) | microblog (Flask+flask_login) |
|---|---|---|---|
| 框架切片 | django/contrib | flask, werkzeug | flask, flask_login, werkzeug |
| store 应用/框架 | 46 / 8 | 60 / 6 | **0** / 25 |
| 主链 | request.session ×54 | session ×12 | session ×22(全在 flask_login) |
| key 证据 | literal 45 + const 5,**0 个 dyn** | literal 13, none 52 | literal 25 |
| 守卫 应用/框架 | 489 / 1002 | 519 / 542 | 44 / 528 |
| 守卫→端点归属 | 132/489 (27%) | 62/519 (12%) | 25/44 (57%) |
| 端点:自带守卫 | 39/63 | 22/57 | 12/26 |
| 端点:并集(守卫∨装饰器) | **55/63 (87%)** | 57/57 | 26/26 |

### 验证点(零词典命中)

- Django `login()` 写点:`django/contrib/auth/__init__.py:193
  request.session[_auth_user_id]`,key 由同文件常量折叠
  (`SESSION_KEY = "_auth_user_id"`)解析,key_status=const。
- CTFd 自管登录写点:`auth.py:16/18 session[id]/[hash]`(应用代码内)。
- microblog 应用代码 **0 个 session 写点**——登录态全部由 flask_login
  代管,框架切片(25 个 store,session 链 ×22)是唯一来源。
  这正面验证了"框架写入静态可见,只要把 venv 纳入扫描范围"。

## 结论

1. **requires 边有源了**:此前 healthchecks 前置条件 2/193(≈1%);
  现在自带守卫或装饰器的视图占 87%(Django,非平凡)。
  Flask 的"装饰器 100%"是平凡的(路由装饰器必然存在),需排除
  路由装饰器后才有意义——待办。
2. **key 全部可解析**(healthchecks 0 个 dyn):常量折叠够用。
3. 守卫→端点直接归属 12%~57%,其余在 helper/model 函数里,
  经调用图归属是下一步(调用图已具备)。
4. CTFd `app.jinja_env.globals` ×43 是真共享 store 但非请求状态——
  链本身即命名空间,消费方可按需过滤,不需要新规则。

## 更正一条旧结论

此前记录"CTFd 端点数为 0 是既有问题"——**测量口径错误**:当时用
`analyze(root,'django')` 扫 Flask 应用。用 `analyze(root,'flask')`
CTFd 得 63 个端点(去重后 57 个视图函数)。django-oscar /
realworld-django-ninja 是真 Django,其端点为 0 的问题维持原判。

## 复现

- 提取器:`stateforge/state/sites.py`(测试
  `stateforge/tests/state/test_sites.py`,7 用例)
- 驱动:`measure.py`(本目录)

## 待办(按优先级;11-10 修正后重排——转移点优先)

1. **转移点扩命名空间**:现在只提 session 下标写;DB 写(`save`/
   `create`/`delete`/`commit`,各应用 100~284 个写点的主力)与
   文件写尚未提。守卫的接收者判定规则也不同(ORM 实例是局部变量,
   "共享+入口形参"资格不适用),需要单独的资格规则。
2. 转移记录:TransitionRecorder 对转移点拍前后快照、落盘复用
   (与 DynamicEdgeCache 同款)。
3. 守卫经调用图归属到端点(直接归属仅 12%~57%)
4. Flask 排除路由装饰器后的非平凡装饰器信号
5. `db.session`/`cache` 链的命名空间语义(链即命名空间,消费侧过滤)

## 端到端实测:session 转移点 抓包验证(CTFd,11-10)

真实请求流(CTFd 自管 Flask 应用,真实 sqlite + 真实 CSRF,8 个包):

```
GET / → GET /login → POST /login 错密码 → POST /login 正确
→ GET /challenges → GET /admin/config → GET /logout → GET /challenges
```

### 结果

- **静态**:12 个 session 转移点(CTFd 自身代码 10 个 + flask 内部 2 个)
- **执行确认 6/12**(write×4 + pop×1 + clear×1):
  `auth.py:16/17/18`(id/nonce/hash 写,即登录转移)、
  `auth.py:35`(session.clear,登出转移)、
  `initialization:398`(CSRF nonce 播种)、`flask/helpers.py:431`(flash 消费)
- **转移记录(快照差)抓到 4 条**:
  - `GET /`:+nonce(匿名态播种)
  - `POST /login`:+id +hash +_permanent,~nonce 轮换 —— **匿名 → 已登录**
  - `GET /logout`:-id -hash -nonce -_permanent —— **已登录 → 匿名**
  - `GET /challenges`(登出后):+nonce 重新播种
- **join 率 8/10**:观测到的 10 个 key 变化事件,8 个映射回静态转移点

### 发现的提取器缺口(真实、具体)

`session.permanent = True`(auth.py:19)——**属性写,不是下标写**,
提取器只认 `x[k]=v`,漏了这一类。flask 把它序列化成 session 里的
`_permanent` key,快照差里看得见(2/10 的缺口全源于它),但 join 不回
静态点。改进项:store 链上的属性赋值(`x.attr = v`)也应记转移点。

### 未执行的 6 个(流程未覆盖,非提取器问题)

update_user×2(需改密码)、reset_password×2、setup 向导×1、flash 写×1。

脚本:`sess_driver_ctfd.py` / `sess_analyze.py`(本目录)。

## 纯静态抓取率测量(11-10,零执行)

基准 = 应用代码里所有 session 链上的状态写(下标写/删/变更调用
+ 属性写)。提取器产出对照基准:

| 应用 | 基准(下标+变更) | 抓到 | 抓取率 | 属性写(缺口) | 框架内另捕获 |
|---|---|---|---|---|---|
| healthchecks | 46 | 46 | 100% | 0 | 8 |
| CTFd | 10 | 10 | 100% | 3 | 2 |
| microblog | 0 | 0 | — | 2 | 22 |
| linkding | 4 | 4 | 100% | 0 | 8 |
| **合计** | **60** | **60** | **100%** | **5** | 40 |

- **下标类 session 转移点:60/60 全中**。资格过滤(共享+入口)
  在 session 链上零损失——它只杀 kwargs 类噪声链,不误伤。
- **属性写 5 个全漏**(`session.permanent = True` 这类):形状缺口,
  与动态实测发现的缺口一致,纯静态即可确认。
- **框架侧 CBV 缺口**:django/contrib 里 `self.request.session`
  的 4 个写点(PasswordResetConfirmView×2、SessionStorage×2)
  因链头 `self` 不是入口形参而不过资格——CBV 方法的入口判定
  是资格规则的待改进项;flask_login 22 个全中。
- key 解析:literal 为主,4 应用合计零 `<dyn>`。

测量备注:首版脚本把 .venv 内 Django 自身代码混进了应用基准
(app 根包含 .venv、框架切片豁免 skip),已修正——基准只算应用代码。
脚本:`static_capture.py`(本目录)。

## 缺口修复(11-10):属性写 + CBV 入口判定

### 修复 1:属性写(生效,全捕获)

`visit_Assign` 增加属性目标分支;属性写过**下标证据门**——仅当
同一链在扫描范围内有别处的下标/变更证据(证明是字典式 store)才
记转移点(`self.title = x` 这类纯属性赋值不认,key_status="attr")。

| 应用 | 修复前 | 修复后 |
|---|---|---|
| CTFd | 10/13(属性写 3 漏) | **13/13 = 100%** |
| microblog | 0/2(属性写 2 漏) | **2/2 = 100%**(证据来自 flask_login 切片) |
| 四应用合计 | 60/65 | **65/65 = 100%** |

### 修复 2:CBV 入口判定(机制就位,真实缺口在更深处)

新增 `make_entry_matcher`:函数名 `Class.method` 按类名匹配端点
(过近似,对资格判定是安全方向),单元测试覆盖。

**但真实应用上数字不变,原因查实了**:4 个漏掉的 django 内部 CBV 写点
(PasswordResetConfirmView×2、SessionStorage×2)所属的类,linkding
**没有路由**(它路由的是 LogoutView/PasswordChangeDoneView);而
linkding 路由的那两个 CBV 也进不了端点表——`_load_defs` 只扫应用根,
venv 里的类对 URLconf 视图解析不可见。所以 CBV 资格缺口实际卡在
**URLconf 视图解析进 venv**——与 oscar/realworld 端点=0 同根因,
是下一个待办,不在本次两个修复内。四个应用自身代码的 CBV
session 写为 0,应用侧无可测对象。

测试:test_sites.py 10 用例;全量 998 通过 / 0 失败。

## 状态面图(11-10):由转移点构造的静态骨架

`state/graph.py::build_state_plane_graph`:转移点 + 读位点 → 二分图。

- **slot 节点**(椭圆)= `链[key]` 不透明原子;`链[*]` = 整库(clear/flush);
  只读 slot 灰点虚线(生产者在扫描范围外)
- **action 节点**(方框)= 函数
- **produces**(实线)= 转移点 → slot,标 op;clear/flush 红色
- **reads**(虚线)= 读位点 → slot;同函数有提前退出守卫的读标蓝
  (读 ≠ 门卫,语义 requires 留给动态派生)

关键实现修正:共享计数**含读**——写于一处、读于另一处恰是跨函数
持久状态的最强信号(生产者/消费者分居两个请求处理函数)。此前只数
写函数,单写多读的链会被误丢。

真实应用:

| | slots | 只读 | actions | produces | reads |
|---|---|---|---|---|---|
| CTFd | 36 | 19 | 67 | 71 | 66 |
| healthchecks | 32 | 5 | 31 | 56 | 22 |

CTFd 真值核对(与 auth.py 源码及动态实测一致):
`login_user` → session[id]/[nonce]/[hash]/[permanent] write;
`logout_user` → session[*] clear;
`authed` → 读 session[id];`get_current_user` → 读 session[id]/[hash]。

渲染:`CTFd_state_plane.png`、`healthchecks_state_plane.png`(本目录)。
动态转移记录(快照差)将来叠加为 slot-state 边即成状态机;本图是骨架。

测试:test_state_graph.py 6 用例;全量 1004 通过 / 0 失败。

## 观测状态机(11-10):静态骨架 × 动态快照差 接通

新增两个纯模块(L1.5,零运行时依赖):

- `state/join.py`:`observe(trigger, before, after, executed_points, chains)`
  → `ObservedTransition`。归因四级:精确 key → 下划线前缀
  (约定匹配,非框架词典)→ clear/flush 承接全部删除 → 如实
  unattributed。快照无变化返回 None(登录失败天然不出边)
- `state/machine.py`:`StateMachine` 按 (from,to,trigger) 去重累积;
  查询接口 `triggers_producing(key)` = 规划入口

CTFd 8 请求实测(每请求独立追踪 + 前后快照):

- **4 条边、3 个状态、零未归因**:
  ∅ --GET /--> {nonce} --POST /login--> {_permanent,hash,id,nonce}
  --GET /logout--> ∅;GET /challenges 回播种边
- **4 个无变化请求不出边**(含错密码——转移不发生,机器正确沉默)
- **`_permanent` 经下划线规则归因到 auth.py:19 session[permanent]**
- **logout 的 4 个删除全部归到 auth.py:35 clear 点**
- **闭环自证**:查询"哪个请求能建立 session[id]" →
  `POST /login`(证据 auth.py:16/17/18/19)。配合静态骨架的
  reads 边(谁读 session[id]),"要到受保护代码先登录"由机器自动推出

渲染:`ctfd_state_machine.png`(本目录)。

测试:test_join_machine.py 8 用例;全量 1012 通过 / 0 失败。

## 跨全部应用跑整条静态链(11-10,19 应用)

端点 → 转移点抓取率 → 骨架 → 路由归属,external/ 全量:

| 应用 | 框架 | 端点(匹配) | 归属函数 | session 转移点 | slots/prod/reads |
|---|---|---|---|---|---|
| CTFd | flask | 63 (62) | 155 | **13/13=100%** | 36/71/66 |
| healthchecks | django | 193 (96) | 161 | **46/46=100%** | 32/56/22 |
| linkding | django | 29 (29) | 206 | **4/4=100%** | 13/15/11 |
| microblog | flask | 27 (27) | 39 | **2/2=100%** | 43/28/55 |
| moin | flask | 88 (87) | 156 | **17/17=100%** | 49/59/33 |
| BenchmarkPython | flask | 2463 (2463) | 2467 | 无写点 | 10/6/12 |
| fava | flask | 15 (15) | 18 | 无写点 | 12/7/14 |
| OctoPrint | flask | 207 (**105**) | 226 | 48/62=77% | 63/89/249 |
| mezzanine | django | 25 (**3**) | 42 | 0/3 | 22/18/7 |
| wger | django | 9 (**3**) | 4 | 0/15 | 16/7/25 |
| django-oscar | django | **0** | 0 | 0/19 | 10/3/10 |
| django-blog-zinnia | django | **0** | 0 | 0/1 | 0/0/0 |
| realworld | django | **0** | 0 | 无写点 | 4/3/4 |
| bakerydemo | django | **1** | 1 | 无写点 | 34/2/34 |
| Spirit | django | 89 (67) | 153 | 无写点 | 5/6/6 |
| trac/Radicale/Predator/atropos | — | 无 django/flask venv,现适配器范围外 | | | |

### 读法:链条的薄弱环节是端点提取,不是新层

依赖链已清晰:**端点质量 → 入口判定 → 转移点资格 → 骨架 → 归属**。
端点正常的 5 个应用,session 转移点全部 100%。端点坏的,后面全断。
三种端点病(全部在适配器,即 URLconf/路由提取层):

1. **include 解析不进 venv**(oscar/zinnia/realworld/bakerydemo,0~1 端点)
   ——已知问题;修它同时能注册 Django CBV 类,顺带解 CBV 资格缺口
2. **mezzanine:路由字段全空**——老式 `url()` + 点分字符串视图
   (`url(r"^login/", "mezzanine.core.views.login")`),适配器不认这种写法
3. **wger:func 字段为空/类名、文件记成 urls.py**——SPA 模式
   (`ReactView.as_view()` 承接所有前端路由)
4. **OctoPrint:一半端点未匹配 + 采进了测试文件**(`csrf_test.py` 的
   `/active`、`/exempt` 成了端点)——适配器卫生问题

### 结论

- sites/graph/attribution 三层在端点正常的应用上全部 100%/高匹配,
  无一失败;此前"别只盯 CTFd"的担心在新层不成立
- **下一个该修的是适配器(端点提取),优先级:include 进 venv >
  mezzanine 老式 url() > wger ReactView > OctoPrint 测试文件**

脚本:`all_apps_chain.py`(本目录)。

## include 修复(11-10):端点提取层四个真 bug

修前诊断(bakerydemo/zinnia 端点 0 的根因),四件事:

1. **venv 路径拼接错**:点分模块名拼成了单文件名(`wagtail.urls.py`),
   应按点分段(`wagtail/urls.py`)——修后 `_venv_module_file`
2. **urls 包不索引**:zinnia 的 `zinnia.urls` 是包(`urls/__init__.py`
   + 子模块 `urls/entries.py`),`rglob("urls.py")` 全漏
3. **老式 `url()` 不认**:walker 只认 `path`/`re_path`,zinnia/mezzanine
   的 `url(...)` 整条跳过(include 都看不见)
4. **`urlpatterns +=` / `i18n_patterns` 漏读**:只认第一条赋值,
   oscar 的主体路由全在 `urlpatterns += i18n_patterns(...)` 里

修复后(唯一 route,func 对):

| 应用 | 修前 | 修后 |
|---|---|---|
| bakerydemo | 1 | **17**(wagtail venv include 全通)|
| django-blog-zinnia | 0 | **42** |
| Spirit | 89 | **149** |
| wger | 9 | 23 |
| mezzanine | 0 有效路由 | 28(其中 5 条有效;点分字符串视图待修)|
| django-oscar | 0 | 2(动态 include `apps.get_app_config(...)`,静态不可解,如实记录)|
| realworld | 0 | 0(django-ninja 自有路由体系,需专用适配器)|
| healthchecks | 97 | 95(+去重;丢的 2 条是空路由垃圾)|

**session 转移点 100% 的应用:5 → 6**(+mezzanine 3/3)。

### 新暴露的缺口(下一个)

- **ICFG 不扫 venv,归属对 venv 视图失效**:bakerydemo 17 端点只匹配
  1——wagtail 视图函数在 site-packages,ICFG(应用根)里没有,
  attribution 的 (file,func) 匹配不到。需要 ICFG 可选 venv 切片
- mezzanine 点分字符串视图(`url(r'^login/', "pkg.views.login")`)
- wger ReactView:23 端点匹配 15,但写 session 的函数仍不是入口

测试:test_django_urlconf_includes.py 7 用例;全量 1025 通过 / 0 失败。
