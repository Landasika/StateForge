# OctoPrint 靶标 NOTES(stateforge/targets/apps20/octoprint)

## 应用与版本

- OctoPrint(3D 打印服务器,Flask 全局 `app` + Tornado WSGI 前置),Python 3.13 venv
  `external/OctoPrint/.venv`(依赖自 setup.py INSTALL_REQUIRES 装)。
- 源码 `external/OctoPrint/src/octoprint`,venv site-packages 内为同内容拷贝
  (diff 仅 `__pycache__` 差异),import 走 site-packages 即可,无需 sys.path 注入。

## 启动方式(与任务描述的差异)

任务描述猜测是 `octoprint.server.createApp(hostCtx=...)` 工厂;**本版本没有**。
实际结构(`src/octoprint/server/__init__.py`):

- 模块级全局 `app = Flask("octoprint")`(server/__init__.py:76),无 app 工厂;
- `class Server.__init__(settings, plugin_manager, connectivity_checker,
  environment_detector, event_manager, ...)` 轻量;`Server.run()` 才做全部装配
  并进入 Tornado `IOLoop.current().start()` 主循环。
- 组件工厂为 `octoprint.init_platform(basedir, configfile, ...)`
  (octoprint/__init__.py:60),返回 (settings, logger, safe_mode, event_manager,
  connectivity_checker, plugin_manager, environment_detector)。

因此 harness = `init_platform` + 手动重放 `Server.run()` 的 **Flask 侧初始化前缀**:

```
_setup_monkey_patching → _setup_mimetypes → _setup_flask_app → _setup_i18n
→ printerProfileManager/eventManager 全局赋值 → _setup_analysis_queue
→ _setup_slicing_manager → _setup_file_manager → LifecycleManager/PreemptiveCache
→ _setup_json_encoding → _setup_connectivity_checker → _setup_plugin_permissions
→ _setup_group_manager → _setup_user_manager → _setup_printer
→ _setup_plugin_manager → log_detected_environment → _setup_jinja2
→ _setup_assets → _setup_timelapse → _setup_command_triggers
→ _setup_login_manager → _setup_blueprints → _check_simple_api_plugins
```

跳过的 run() 后半段(全部 Tornado/网络/后台部分):intermediary server 绑定端口、
`_start_event_loop`、`_setup_tornado_app`、`_initialize_and_bind_server`(监听 socket)、
STARTUP 事件、analysis backlog、printer autoconnect、watched observer( watchdog 线程)、
shutdown handlers、IOLoop 主循环。Flask test_client 只需要前缀,请求面完全可复现。

### 关键坑(已修)

1. **`octoprint.server.pluginManager` 模块全局必须手动赋值**:run() 早期
   `pluginManager = self._plugin_manager`;漏掉它则 `views.py`/`api/__init__.py`
   里 `pluginManager.get_hooks(...)` → NoneType 500,炸掉 `/`、`/cached.gif`、
   `/api/settings/templates`、`api/setup/wizard` 四条路由。同理补 `debug`/`safe_mode`。
2. **不能跑完整 Server.run()**:会绑定端口 + 起 Tornado IOLoop,fuzzer 进程挂死。
3. settings 极简 config.yaml 必须:`server.firstRun: false`(否则 `/` 302 到
   first-run wizard)、`server.onlineCheck.enabled: false`(禁 connectivity 轮询)、
   `devel.enableRateLimiter/enableCsrfProtection: false`(fuzz 高频请求/免 CSRF)、
   `devel.webassets.bundle: false` + `devel.cache.*: false`(免 webassets 打包与
   preemptive 渲染缓存,快且无副作用)。
4. 仍会残留的 daemon 线程(可接受,event bus worker、timelapse render queue、
   15min 心跳 timer,均 daemon=True,不阻塞进程退出)。
5. 基于进程隔离:basedir 用 `mkdtemp(prefix=f"octoprint_sf_{pid}_")`,
   users.yaml/logs/generated 全部落临时目录,互不串。

## 登录引导(API key)

- `octoprint.server.userManager.add_user(username, password, active=True,
  permissions=[Permissions.ADMIN], groups=[], apikey=<固定 hex>)` ——
  FilebasedUserManager 支持 apikey 直挂(`octoprint/access/users.py:542`),
  落盘 basedir/users.yaml。
- 请求侧:任意请求带 `X-Api-Key: <key>` 头 → `load_user_from_request`
  (server/__init__.py:230)→ `util.get_api_key`(server/util/__init__.py:355,
  也认 `?apikey=` 与 `Authorization: Bearer`)→ `get_user_for_apikey` 反查用户。
  passive login 生效(fresh session 不需要),无 CSRF 面子。
- 备选:POST /api/login {"user","pass"} 拿 session cookie;或 {"passive":true}
  探测当前身份。harness 选 API key(对 fuzzer 的 header 注入最友好)。

## 跨请求守卫实例(≥2,实测状态码)

1. **API key/会话门(403/302)**:anonymous 或坏 key(`X-Api-Key: deadbeef`)下
   `/api/settings`、`/api/version`、`/api/job`、`/api/printer` → **403**
   (api.enabled=true 时整个 /api 要求认证);`/` → **302** 跳 `/login`(accessControl
   enabled)。带 admin key 后 `/api/settings` → 200。
2. **连接状态门(printer connected 才有深块)**:`/api/printer` 带 admin key 仍
   **409** `{"error": "Printer is not operational"}`(tool/bed/chamber 同);
   `/api/job` 返回 200 但浅块 `state: "Offline"`、file/progress 全 None —— 深块
   (file name、completion、printTimeLeft)只在连接并开始打印后出现;
   `/api/connection` 的 `current.state: "Offline"` ↔ POST /api/connection
   (port+baudrate)后才转 "Operational",构成 OFFLINE→CONNECTING→OPERATIONAL
   状态机。
3. **文件域(uploaded 文件状态)**:文件经 POST /api/files/local 上传进
   LocalFileStorage(basedir/uploads),进入 fileManager 的文件表 + 分析队列;
   `GET /api/files/local/<path>` 对不存在的文件 **404**;`/api/files` 列表里
   每个文件带 prints(failure/success/last)统计与 analysis(estimatedPrintTime)
   字段 —— 同一路由的响应块随文件上传/打印历史演化。printer 选中文件
   (POST /api/files/local/<path> {"command":"select"})后 /api/job 才引用该文件。

其他门:首访 wizard(firstRun=true 时 `/` 302 → /wizard,seenWizards 记忆)、
appkeys 工作流(用户级 API key 授权 pending 决策)、`/api/settings/reauthReq`
(敏感设置要求 fresh login,remember-me 会话不够)。

## 路由面板

- url_map 共 **175 条 rule**(含插件 blueprint:`/plugin/logging/logs`、
  `/plugin/pluginmanager/plugins`、`/plugin/softwareupdate/...`、
  `/plugin/backup/...` 等;SockJS/socket 路由在 Tornado 层,Flask url_map 无)。
- harness ROUTES 32 条(全部 GET、带 X-Api-Key):UI 面(/、/login、/recovery、
  /robots.txt、/cached.gif)+ API 面(version/server/printer*/job/connection/files/
  settings*/access*/timelapse/languages/printerprofiles/slicing/system*/setup/
  currentuser)。
- 本版本无 `/turtle/`、`/downloads/logs/`、`/api/users`(旧版 OctoPrint 才有;
  users 现为 `/api/access/users`,日志下载在 `/plugin/logging/logs/<path>`)。

## smoke 结果

`external/OctoPrint/.venv/bin/python -m stateforge.targets.apps20.octoprint.harness`

```
smoke: 32/32 non-5xx
  200 / , 302 /login /recovery(已登录重定向), 200 robots.txt/cached.gif,
  200 全部 /api/*,其中 /api/printer /api/printer/tool /api/printer/bed 为
  409(Printer is not operational —— 连接状态门,非 5xx,符合验收)
```

## 门3 数字(主环境 python,src/ 子目录)

```
icfg fn=4150 blocks=34598 edges=39123 | ep=102 cand=105
```

(exit 0,未超时;工作目录 /home/landasika/icassp,`build_icfg(Path('external/OctoPrint/src'))`。)
