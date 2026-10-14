# CTFd 状态语义笔记(门 4)

应用:CTFd(CTF 竞赛平台,Flask 3 + SQLAlchemy,生产级)。批 2。

## 跨请求守卫实例(≥2)

### G1 会话域:CTFd 自管 session auth
- 位置:CTFd.utils.user.get_current_user — session["id"] +
  session["hash"]=hmac(password) 双校验(密码改 → 会话失效,状态绑定)
- 生产者:POST /login(凭据核验后建会话);harness 经 session_transaction
  直构(id/name/type/hash)
- 未登录:/challenges 等 → 302;/api/v1/* → 401/302

### G2 DB 域:setup 向导状态(全局状态门)
- 位置:CTFd.views — is_setup() 检查 Configs["setup"]
- 未完成 → 全站 302 /setup(302 风暴,实测);完成 → 正常路由
- 生产者:POST /setup(向导写 Configs);harness 经 set_config 直构
  (绕缓存:set_config 才失效 config cache,直插 DB 无效——实测教训)

### G3 权限域:admin 门
- /admin/* 家族:非 admin 会话 → 302;admin → 200(实测 8 页 200)
- Users.type 字段持久状态

### G4 DB 域:挑战可见性/冻结
- challenge 状态机(hidden/visible)、freeze 窗口、提交判分
  (submissions 表):深块依赖挑战创建 + 可见性配置链

## 状态域覆盖
- SESSION:G1;DATABASE:G2(Configs)/G4(challenges/submissions);RIGHTS:G3

## 接入门记录(2026-10-14)
- 门 1:28/28 non-5xx(200×23 含 admin/API 真实渲染;302 为登录/
  setup 重定向;404 为默认主题未含的静态页)
- 门 2:DATABASE_URL env 每进程 sqlite ✓
- 门 3:icfg 841fn/7832blk/9049e | ep=63 cand=116
- venv:external/CTFd/.venv(requirements.txt 冻结清单)
