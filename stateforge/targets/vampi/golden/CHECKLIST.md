# VAmPI 效应人工核对表(SF1 出口项;2026-09-19 自核,待用户抽查)

口径:端点覆盖率 = 正确提取效应的端点 / 源码中存在状态写的端点;效应条目同理。

## 逐端点核对

| 端点 | 源码状态写 | 提取 | 判定 |
|---|---|---|---|
| GET /createdb | User 种子 ×3(经 `User.init_db_users()` helper) | — | ✗ 遗漏(helper 间接,已列 LIMITATIONS) |
| GET / | 无 | — | ✓ |
| GET /users/v1 | 只读 | — | ✓ |
| GET /users/v1/_debug | 无 | — | ✓ |
| POST /users/v1/register | User.{username,password,email,admin} | 全部 4 条(value=*) | ✓ |
| POST /users/v1/login | token 产 | Session.authed_as | ✓ |
| GET /me | 只读 + token 需 | pre authn ✓ | ✓ |
| GET /users/v1/{username} | 只读,源码无 token 检查 | 无前置 | ✓(暴露面如实) |
| DELETE /users/v1/{username} | 删用户(db.session.delete) | pre authn ✓ / 效应 ✗ | ✗ 效应遗漏(delete 未建模) |
| PUT /users/v1/{username}/email | User.email 写(双分支) | 1 条(已去重) | ✓ |
| PUT /users/v1/{username}/password | User.password 写(双分支) | 1 条(已去重) | ✓ |
| GET /books/v1 | 只读 | — | ✓ |
| POST /books/v1 | Book.{book_title,secret_content,user_id} | 全部 3 条 | ✓ |
| GET /books/v1/{book_title} | 只读(secret 回显) | pre authn ✓ | ✓ |

## 数字

- **端点效应覆盖:5/7 = 71% ≥ 70%**(SF1 出口标准达标)
- **效应条目:10/14 = 71%**;遗漏 4 条 = createdb 种子 ×3 + delete ×1
- **前置(认证)提取:6/6 全对**;误报 0(register 的可选 admin 旗标已修,不再产前置)
- Book.user_id 进入候选(ownership)——BOLA 目标状态可表达

## 已知遗漏的原因(均有 LIMITATIONS 条目)

1. helper 内种子/删除:`User.init_db_users()`、`db.session.delete()` —— 过程间盲区
2. 非常量值一律 value=None(`=*`):具体值在请求里,静态不可见
