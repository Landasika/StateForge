# PyGoat 状态语义笔记(门 4)

应用:OWASP PyGoat(Django 4.2 + allauth + sqlite,VWA 教学平台)。批 1。

## 跨请求守卫实例(≥2)

### G1 会话域:auth_lab 登录链
- `GET /auth_lab/login` → `POST` 核验 → 受保护 lab 视图(未登录跳转)
- 生产者:`/auth_lab/signup`(注册写 UserInfo 表)
- allauth:`/accounts/login//signup/` 同族会话门

### G2 DB 域:挑战完成状态(UserInfo)
- 每个挑战视图检查/写入 UserInfo 的 lesson 状态(XSS/SQL/XXE... lab 族)
- 跨请求:解一个 lab 改一次状态,仪表盘按状态渲染

### G3 会话+DB 复合:Broken Access Control(ba/ba_lab)
- ba_lab 角色门:非特权用户访问受限视图(有意漏洞面,守卫谓词可提取)

## 状态域覆盖
- SESSION:G1(allauth);DATABASE:G2(UserInfo);FILE:无

## 接入门记录(2026-10-14)
- 门 1:24/24 non-5xx(superuser 会话驱动)
- 门 2:SF_DB 覆写 settings.DATABASES 每进程 sqlite ✓
- 门 3:见 gates 表(后台运行中,回填)
- venv:targets/pygoat/.venv(cffi/argon2-cffi 需升级适配 py3.13;Pillow
  旧 pin 弃用装新版)
