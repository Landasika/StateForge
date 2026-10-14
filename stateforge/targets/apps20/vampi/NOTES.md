# VAmPI 状态语义笔记(门 4)

应用:VAmPI(Vulnerable Modern API,Flask 2.2 + connexion + SQLAlchemy,
OWASP 名录 VWA)。批 1;boot 经 targets/vampi/adapter.make_app()
(环境变量先于 import;import config 避免 app↔config 循环;cwd 钉
VAMPI_DIR——connexion specification_dir 相对解析)。

## 跨请求守卫实例(≥2)

### G1 Token 域:JWT 签发/校验(tokentimetolive TTL)
- 生产者:`POST /users/v1/login`(admin/pass1 等种子用户 → auth_token)
- 消费者:`GET /me`、`POST /books/v1`、email/password PUT(401 无 token)
- 谓词:`Token.valid(username) ∧ TTL 内`

### G2 DB 域:用户存在性(BOLA 族)
- `GET /users/v1/{username}`:按 username 查 User 表,404 不存在
- 生产者:`POST /users/v1/register`
- resourcelevel 控制流:vulnerable 旗标切换分支(有意漏洞面)

### G3 DB 域:books 资源生命周期
- `GET /books/v1/<title>`:401 门 + 书目存在性;生产者 POST /books/v1
- createdb(GET)整体重置:drop_all→create_all→seed(状态原点)

## 已知怪癖(如实记录)
- email/password 端点与 login 错误路径返回 None → connexion 响应校验 400
  (VAmPI 自身 bug,故意漏洞应用;深块可达性以此为门)

## 状态域覆盖
- SESSION(Token):G1;DATABASE:G2(users)/G3(books);FILE:无

## 接入门记录(2026-10-14)
- 门 1:14/14 non-5xx(404/401/400 均为合法门响应)
- 门 2:sqlite database/database.db;进程隔离经 createdb 重置 + (SF_DB
  演进项:settings 覆写,BP 系同法)
- 门 3:icfg 46 fn/424 blocks/492 edges;appmodel(connexion) 14 ep/16 cand
- venv:targets/vampi/.venv(sqlalchemy 需 ≥2.0.36 适配 py3.13)
