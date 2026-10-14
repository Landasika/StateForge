# wger 状态语义笔记(门 4)

应用:健身/训练/营养管理(Django 6.0,生产级,sqlite)。批 2。
URL 全挂 i18n_patterns 前缀(`/en/...`)。

## 跨请求守卫实例(≥2)

### G1 会话+DB 复合:@login_required + Routine 存在性/属主
- 位置:`wger/manager/views/routine.py:42-55`(`copy_routine`:
  `@login_required` + `get_object_or_404(Routine, pk=pk)` +
  `request.user != routine.user and not routine.is_public` 属主门);
  view/edit/logs 同款(各视图 `@login_required`)
- 谓词:`Session.authed ∧ EXISTS Routine[id] ∧ (owner==user ∨ is_public)`
- 生产者:`POST /en/user/login`(`wger/core/views/user.py:752`
  WgerLoginView,allauth)/ `POST /en/routine/add`
- 单请求 fuzz 不可达:未登录 302;pk 不存在 404

### G2 会话域:guest 用户按路径即时建号(中间件隐式登录)
- 位置:`wger/utils/middleware.py:53-70` `get_user` —
  `check_current_request`(`:37-48`,`SPECIAL_PATHS=('dashboard',`:33)
  命中 + GET + 未登录 → `create_temporary_user` + `django_login`
- 谓词:`Session.authed == True`(由 GET /en/dashboard 隐式产生)
- 生产者:`GET /en/dashboard` 本身(匿名首击即建号并写会话)
- 跨请求:同一会话后续请求以 guest 身份通过 G1 门;状态完全藏于 session

### G3 会话域:trainer.identity 会话重绑定(权限提升链)
- 位置:`wger/core/views/user.py:196` `trainer_login` —
  读/写 `request.session['trainer.identity']`(`:205` 读,`:245` 删),
  要求 `gym.gym_trainer` 权限;消费端 `routine.py:51-55` 复制他人
  routine 时校验 `session.get('trainer.identity')`
- 谓词:`Session['trainer.identity'] == routine.user.pk ∧ user.has_perm(gym_trainer)`
- 生产者:`POST /en/user/<pk>/trainer-login`(POST-only,经 CSRF)
- 单请求不可伪造:需既有 trainer 权限 + 会话键,典型跨请求状态原子

### G4 DB 域:NutritionPlan 按 uuid 定位 + 属主
- 位置:`wger/nutrition/views/plan.py:72/118` —
  `get_object_or_404(NutritionPlan, pk=pk, user=request.user)`
- 谓词:`EXISTS NutritionPlan[uuid] ∧ owner==user`(uuid7 主键)
- 生产者:`POST /en/nutrition/add`(营养计划创建)
- 状态迁移:plan meals/log 聚合跨请求累积(` nutritional values`)

## 状态域覆盖
- SESSION:G1(登录)、G2(guest 隐式登录)、G3(trainer.identity 重绑定)
- DATABASE:G1(Routine 存在/属主)、G4(NutritionPlan uuid/属主)、
  WeightEntry/WorkoutSession 日志类追加迁移
- FILE:MEDIA_ROOT 上传(exercise 图/视频,easy_thumbnails 缩略图)

## 接入门记录(2026-10-14)
- 门 1:40/40 non-5xx(冒烟表 harness.smoke();含 302 守卫正常响应)
- 门 2:SF_DB 每进程 sqlite(pid 后缀,支持 env 覆写),双进程不串库 ✓
- 门 3:build_icfg 1226 fn/9219 blocks/9964 edges;analyze(django)
  9 ep/9 cand(主环境 python,未超时)
- venv:external/wger/.venv(Django 6.0.9);坑:wger/__init__.py 无条件
  import celery(deps-only 安装缺 celery+flower,补装 `pip install
  --no-cache-dir celery[redis] flower`);migrate 不种基础数据 → 须
  loaddata languages/licenses/setting_*_units,否则建 user 即
  IntegrityError(UserProfile FK Language);context_processor 硬编码
  GymConfig.objects.get(pk=1)(wger/utils/context_processor.py:106),
  须显式建 pk=1 否则全站模板 500;settings.main 面向 postgres,改用
  settings.local_dev(sqlite + ALLOWED_HOSTS=['*'] + USE_CELERY=False)
