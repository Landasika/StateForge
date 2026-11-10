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
