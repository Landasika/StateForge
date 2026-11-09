# 驱动脚本说明

各应用的驱动脚本是临时文件(未入库),因为每个应用都需要它自己的虚拟环境和
初始化方式。采集方式统一为:

    <app>/.venv/bin/python <trace_collect.py> <app_root> <driver.py> <out.json>

其中 `trace_collect.py` 是纯标准库的 tracer 加载器(按路径加载
`stateforge/runtime/tracer.py`),因此不需要往应用虚拟环境里安装 stateforge。

| 应用 | 入口 | 驱动方式 |
|---|---|---|
| microblog | `app/__init__.py` | Flask `test_client()`,匿名访问 |
| CTFd | `CTFd/__init__.py` | `create_app()` + 独立 sqlite + `db.create_all()`,管理员登录后访问 |
| realworld-django-ninja | `config/settings.py` | Django `Client`,`DiscoverRunner.setup_databases()` |
| linkding | `bookmarks/settings/dev.py` | 同上,`LD_DISABLE_BACKGROUND_TASKS=True` |
| healthchecks | `hc/settings.py` | 同上,`DB=sqlite`、`ALLOWED_HOSTS=*`、locmem 邮件后端 |
| django-oscar | `sandbox/settings.py` | 同上,需把 `sandbox/` 加入 `PYTHONPATH` |
| fava | `fava.application.create_app` | Flask `test_client()`,临时账本;路由前缀 `/beancount/` |
| Radicale | `radicale.Application` | Werkzeug `Client` 包 WSGI,PROPFIND/PUT/MKCALENDAR |

## 两个需要注意的坑

1. **`fava` 是拷贝安装到 site-packages**(不是 editable)。分析根和轨迹过滤都
   必须指向 `.venv/lib/python3.13/site-packages/fava`,否则一条项目内事件都
   留不下。
2. **Django 的 `setup_databases()` 必须在 tracer 之外执行**,否则迁移和建表
   会产生几十万条与业务无关的事件。
