# Spirit 状态语义笔记(门 4)

应用:Spirit(Django 论坛,生产级)。批 2;库式应用——官方
`spirit startproject` 脚手架生成宿主(每进程临时目录),settings=
spirit_host.settings.dev。

## 跨请求守卫实例(≥2)

### G1 会话域:登录门
- 位置:spirit.user(登录/注册/密码流);user/login/
- 谓词:`Session.authed`;未登录访问受限页 → 302 login(实测
  /user/login/ 在已登录时 302,注册/重发激活 302)

### G2 DB 域:论坛层级树(分类→主题→楼层)
- 位置:spirit.category/topic/comment
- 谓词:`EXISTS Topic[category_id]`(发帖深块前置)+ 楼层归属
- 生产者:topic/publish/<category_id>(两级构造:先分类后主题)

### G3 权限域:st/admin 后台全家
- /st/admin/*:moderator/admin 门(实测 superuser 会话 200×8;
  普通用户 403);topic pinned/closed 状态迁移动作在此

### G4 DB 域:主题状态位
- topic is_pinned/is_closed/global(置顶/关闭深块由状态迁移动作开启)

## 状态域覆盖
- SESSION:G1;DATABASE:G2(层级树)/G4(状态位);RIGHTS:G3

## 接入门记录(2026-10-14)
- 门 1:24/24 non-5xx(200×18 含 st/admin 八页真实渲染;
  405=方法门;302=登录态门)
- 门 2:每进程 mkdtemp 脚手架 + SF_DB 覆写,天然隔离
- 门 3:见 gates 汇总表(后台批跑)
- venv:external/Spirit/.venv(Spirit 0.14.3 源码 -e;
  官方口径 py≤3.11/Django≤4.2,实测 py3.13/Django6 可运行——
  如实记录,长期观察运行时怪癖)
- 踩坑:startproject 需 venv bin 上 PATH(内部调 django-admin);
  settings 模块名 dev 非 developer;ALLOWED_HOSTS 放行 testserver
