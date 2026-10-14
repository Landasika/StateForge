# Radicale 状态语义笔记(门 4)

应用:Radicale(CalDAV/CardDAV 服务器,生产级,纯 WSGI,文件存储)。
批 3;经 stateforge/adapters/wsgi.py(werkzeug test Client 直驱 WSGI)
接入——WSGI 薄适配层首靶。

## 跨请求守卫实例(≥2)

### G1 认证域:BasicAuth 门
- 位置:radicale.auth.htpasswd(harness 配置:plain 用户种子)
- 谓词:`Authorization == Basic <user:pass>`;未带/错 → 403
- 生产者:htpasswd 文件状态(跨请求的凭证持有)

### G2 文件域:CalDAV 集合存在性(MKCOL→PROPFIND/REPORT)
- 位置:radicale.storage.read_only/Collection 按 path 定位
- 谓词:`EXISTS Collection[/user/cal/]`
- 生产者:`MKCOL /user/cal/`(harness 实测 403:owner_only 权利模型
  需更细的 rights 配置才放行 W;403 本身即权利门真实生效的证据)

### G3 文件域:事件资源读写对(PUT→GET/DELETE)
- 位置:radicale.storage
- 谓词:`EXISTS Event[/user/cal/evt.ics]`
- 生产者:`PUT .../evt.ics`(409 = 集合不存在的先序依赖:两级
  状态构造链的典型样本:先集合后资源)

### G4 权利域:owner_only 跨用户访问
- `GET /other-user/...` → 403(实测):跨主体访问被权利模型拦截

## 状态域覆盖
- SESSION(凭证):G1;FILE:G2/G3(集合/事件树);RIGHTS:G4

## 接入门记录(2026-10-14)
- 门 1:22/22 non-5xx(207=CalDAV 多状态真实响应;403/404/409 均为
  合法门响应)
- 门 2:每进程 mkdtemp 存储目录,天然隔离
- 门 3:见 gates 汇总表
- venv:external/Radicale/.venv(+werkzeug 供 WSGI 驱动)
