"""OctoPrint 靶 harness(Flask 全局 app + Tornado 前置的 3D 打印服务器,批 2)。

启动:临时 basedir + 最小 config.yaml → octoprint.init_platform(建 settings/
plugin manager/event bus/connectivity/environment)→ 手动重放 Server.run() 的
Flask 侧初始化(_setup_flask_app … _setup_blueprints,跳过 Tornado/intermediary
server/IOLoop/后台观察线程)→ FilebasedUserManager 造 admin+apikey,test_client
带 X-Api-Key 访问。

状态语义(NOTES.md 详):
- 会话/API key 域:anonymous → user → admin 的三级权限门;
- 打印机域:printer connected 才有 job 深块/命令端点;
- 文件域:uploads 文件夹的 uploaded/printed 状态 + 分析队列。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[4] / "external" / "OctoPrint"

_BASEDIR = Path(tempfile.mkdtemp(prefix=f"octoprint_sf_{os.getpid()}_"))
_API_KEY = "sf" + os.urandom(16).hex()

(_BASEDIR / "config.yaml").write_text(
    """
server:
  firstRun: false
  secretKey: sf-probe-secret-key
  ignoreIncompleteStartup: true
  startOnceInSafeMode: false
  onlineCheck:
    enabled: false
  ipCheck:
    enabled: false
api:
  enabled: true
  allowCrossOrigin: false
accessControl:
  enabled: true
  salt: sf-probe-salt
  userManager: octoprint.access.users.FilebasedUserManager
  groupManager: octoprint.access.groups.FilebasedGroupManager
  trustBasicAuthentication: true
devel:
  enableRateLimiter: false
  enableCsrfProtection: false
  webassets:
    bundle: false
  cache:
    enabled: false
    preemptive: false
""",
    encoding="utf-8",
)

import octoprint as _octoprint  # noqa: E402

(
    _settings,
    _logger,
    _safe_mode,
    _event_manager,
    _connectivity_checker,
    _plugin_manager,
    _environment_detector,
) = _octoprint.init_platform(
    basedir=str(_BASEDIR),
    configfile=str(_BASEDIR / "config.yaml"),
    debug=False,
    verbosity=0,
    disable_color=True,
)

import octoprint.server as _srvmod  # noqa: E402
from octoprint.server import Server, LifecycleManager  # noqa: E402
from octoprint.server.util.flask import PreemptiveCache  # noqa: E402
from octoprint.printer.profile import PrinterProfileManager  # noqa: E402
from octoprint.systemcommands import system_command_manager  # noqa: E402

_server = Server(
    settings=_settings,
    plugin_manager=_plugin_manager,
    connectivity_checker=_connectivity_checker,
    environment_detector=_environment_detector,
    event_manager=_event_manager,
    allow_root=True,
)

# ~~ 重放 Server.run() 的 Flask 侧初始化(Tornado/网络部分全部跳过)
APP = _srvmod.app
_srvmod.pluginManager = _plugin_manager  # run() 早期赋值的模块级全局,缺它则 views/api 全挂
_srvmod.debug = False
_srvmod.safe_mode = _safe_mode
_server._setup_monkey_patching()
_server._setup_mimetypes()
_server._setup_flask_app(APP)
_server._setup_i18n(APP)

_srvmod.printerProfileManager = PrinterProfileManager()
_srvmod.eventManager = _event_manager
_server._setup_analysis_queue()
_server._setup_slicing_manager()
_server._setup_file_manager()
_srvmod.pluginLifecycleManager = LifecycleManager(_plugin_manager)
_srvmod.preemptiveCache = PreemptiveCache(
    os.path.join(_settings.getBaseFolder("data"), "preemptive_cache_config.yaml")
)
_server._setup_json_encoding()
_server._setup_connectivity_checker()
_srvmod.environmentDetector = _environment_detector

_components = {
    "plugin_manager": _plugin_manager,
    "printer_profile_manager": _srvmod.printerProfileManager,
    "event_bus": _event_manager,
    "analysis_queue": _srvmod.analysisQueue,
    "slicing_manager": _srvmod.slicingManager,
    "file_manager": _srvmod.fileManager,
    "plugin_lifecycle_manager": _srvmod.pluginLifecycleManager,
    "preemptive_cache": _srvmod.preemptiveCache,
    "json_encoder": _srvmod.jsonEncoder,
    "json_decoder": _srvmod.jsonDecoder,
    "connectivity_checker": _connectivity_checker,
    "environment_detector": _environment_detector,
    "system_commands": system_command_manager(),
}
_server._setup_plugin_permissions()
_server._setup_group_manager(_components)
_components.update(group_manager=_srvmod.groupManager)
_server._setup_user_manager(_components)
_components.update(user_manager=_srvmod.userManager)
_server._setup_printer(_components)
_components.update(printer=_srvmod.printer)
_server._setup_plugin_manager(_components)
_environment_detector.log_detected_environment()
_server._setup_jinja2()
_server._setup_assets()
_server._setup_timelapse()
_server._setup_command_triggers()
_server._setup_login_manager()
_server._setup_blueprints()
_server._check_simple_api_plugins()

# ~~ 登录引导:FilebasedUserManager 直接造 admin 用户并挂固定 API key
from octoprint.access import permissions as _permissions  # noqa: E402

_USER, _PASS = "probe", "Passw0rd!"
try:
    _srvmod.userManager.add_user(
        _USER,
        _PASS,
        active=True,
        permissions=[_permissions.Permissions.ADMIN],
        groups=[],
        apikey=_API_KEY,
    )
except Exception:
    _srvmod.userManager.add_user(
        _USER,
        _PASS,
        active=True,
        permissions=[_permissions.Permissions.ADMIN],
        groups=[],
        overwrite=True,
    )

AUTH_HEADERS = {"X-Api-Key": _API_KEY}

CLIENT = APP.test_client()

ROUTES: list[tuple[str, str, str]] = [
    ("GET", "/", "public"),                        # UI 入口(firstRun=false 免 wizard 重定向)
    ("GET", "/login", "public"),                   # 登录页
    ("GET", "/recovery", "public"),                # 恢复模式页
    ("GET", "/robots.txt", "public"),
    ("GET", "/cached.gif", "public"),
    ("GET", "/api/version", "apikey"),             # 无权限要求
    ("GET", "/api/server", "apikey"),
    ("GET", "/api/printer", "session"),            # 需要 STATUS + printer connected
    ("GET", "/api/printer/tool", "session"),
    ("GET", "/api/printer/bed", "session"),
    ("GET", "/api/printer/sd", "session"),
    ("GET", "/api/printer/command/custom", "apikey"),
    ("GET", "/api/printer/error", "session"),
    ("GET", "/api/job", "session"),                # printer connected 才有深块
    ("GET", "/api/connection", "apikey"),
    ("GET", "/api/files", "apikey"),
    ("GET", "/api/files/local", "apikey"),
    ("GET", "/api/settings", "admin"),
    ("GET", "/api/settings/templates", "admin"),
    ("GET", "/api/settings/reauthReq", "admin"),
    ("GET", "/api/access/users", "admin"),
    ("GET", "/api/access/users/probe", "admin"),
    ("GET", "/api/access/groups", "admin"),
    ("GET", "/api/access/permissions", "admin"),
    ("GET", "/api/timelapse", "admin"),
    ("GET", "/api/languages", "admin"),
    ("GET", "/api/printerprofiles", "admin"),
    ("GET", "/api/slicing", "admin"),
    ("GET", "/api/system/commands", "admin"),
    ("GET", "/api/system/usage", "admin"),
    ("GET", "/api/setup/wizard", "admin"),
    ("GET", "/api/currentuser", "session"),
]


def smoke() -> list[tuple[str, int | str]]:
    out: list[tuple[str, int | str]] = []
    for method, path, _auth in ROUTES:
        try:
            r = CLIENT.get(path, headers=AUTH_HEADERS)
            out.append((f"{method} {path}", r.status_code))
        except Exception as exc:                 # noqa: BLE001
            out.append((f"{method} {path}", f"EXC:{type(exc).__name__}:{exc}"))
    return out


def dump_routes() -> list[str]:
    """dump Flask url_map 的真实路由(带参数的用种子替换)。"""
    seeded: list[str] = []
    for rule in sorted(APP.url_map.iter_rules(), key=lambda r: r.rule):
        path = rule.rule
        for arg, converter in rule._converters.items():
            if arg == "path":
                path = path.replace(f"<path:{arg}>", "seed/path.gcode")
            elif converter == "string":
                path = path.replace(f"<string:{arg}>", "seed")
            else:
                path = path.replace(f"<{arg}>", "1")
        seeded.append(f"{sorted(rule.methods)} {path}")
    return seeded


if __name__ == "__main__":
    results = smoke()
    ok = sum(1 for _, s in results if isinstance(s, int) and s < 500)
    print(f"smoke: {ok}/{len(results)} non-5xx")
    for route, status in results:
        print(f"  {status!s:>4}  {route}")
