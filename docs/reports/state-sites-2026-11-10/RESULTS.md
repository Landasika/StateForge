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
4. 原子不透明:slot 就是 `链["key"]`,不产 "auth" 之类语义名;
   因果边(写点 ⇒ 守卫翻转)留给动态确认。

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

## 待办(按优先级)

1. 守卫经调用图归属到端点(12%→?)
2. Flask 排除路由装饰器后的非平凡装饰器信号
3. 写点 ⇒ 守卫的动态因果确认(闭环第三环)
4. `db.session`/`cache` 链的命名空间语义(链即命名空间,消费侧过滤)
