# 真实应用端到端:goal_app_bridge + http_plan_executor(linkding 判达)

日期:2026-10-15。第一靶:linkding(生产级 Django 书签管理)。
从"20 真实应用靶标"池打的第一枪:**目标块自动推导 → 真实 HTTP 执行
→ 块级判达验证**全链通。

## 新增(零修改既有文件)

| 模块 | 职责 |
|---|---|
| `state/goal_app_bridge.py` | 阶段一(主环境):analyze → **Django 框架补全**(`@login_required` 装饰器 → 需 auth;`django.contrib.auth` urls include → 内置 LoginView 产 auth——框架结构词汇,零应用特定,LLM 无 key/缓存时的兜底)→ 目标块+必要守卫 → AppModel 端点前置约束 → plan_backward 链 + final-action |
| `runtime/http_plan_executor.py` | 阶段二(应用 venv):harness test_client 真实 HTTP 执行;登录步凭据=环境知识(harness 契约,密码不可从源码推导);路径槽种子渲染;TraceSession 轨迹 → 块级判达;**trace_root 收窄**(视图目录)防渲染链拖垮采集 |

两段式架构 = 依赖隔离的事实(静态分析无需应用依赖,执行在应用 venv;
中间 JSON 序列化走既有 pydantic/save_program_ir 通道)。应用 venv 补装
stateforge 本体(--no-deps)+ pydantic/networkx/PyYAML = apps20 执行面
标配。

## linkding 实测

```
目标:edit 视图 GET 路径深块 239-241(含 access.bookmark_write 归属查询)
约束:[Session.authed_as truthy](@login_required 结构补全)
链:  [POST /login/, GET /bookmarks/<id>/edit](final-action 自动补尾)
执行:POST /login/ → 302(真实 Django 登录)
     GET /bookmarks/1/edit → 200,tgt=True(239-241 块覆盖)
target_covered: True
```

## 踩坑留档

- 相对路径 × cwd:app_root 必须 resolve(阶段一曾在应用 cwd 下静默产
  空模型);
- Django urlpattern 无前导斜杠,路径渲染补 /;
- `?id` 槽 MVP 用种子 "1"(harness 预置书签);资源槽完整绑定留后续;
- settrace 采全库在真实渲染链下超时(15 分钟不归),trace_root 收窄到
  视图目录后秒级——判定不受影响(目标块在视图内)。

## 诚实边界

- POST 保存分支(form.save())需 Django 表单字段词汇(BookmarkForm
  必填字段提取),留后续;
- ORM 内联归属谓词(`owner=request.user`)词面不可提,由执行期块覆盖
  裁决(目标块含归属查询调用);
- 认证凭据来自 harness 契约(probe/Passw0rd!),非推导——密码本就
  不可从源码推导。

## 验收

tests/evaluation/test_real_app_goal.py(2 例,subprocess 两段式,
linkding venv 缺席时 skip);架构边界 5/5。
