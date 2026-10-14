# django-oscar sandbox 状态语义笔记(门 4)

应用:Oscar 官方演示电商(Django + oscar 电商框架,sandbox 配置)。
批 2;**订单/支付流 = create→pay→ship 主张的旗舰同构靶**。

## 跨请求守卫实例(≥2)

### G1 会话域:账户登录门
- 位置:oscar.apps.checkout.views.PaymentDetailsView @method_decorator(
  login_required) 等全家
- 谓词:`Session.authed`;未登录 → 302 /accounts/login/
- 生产者:POST /accounts/login/(superuser 种子)

### G2 DB 域:basket→checkout→order 状态机(订单流)
- 谓词:`Basket.lines > 0`(checkout 深块前置)
- 生产者:POST /basket/add/<product>(目录产品先行)→
  shipping-address → payment → order-placement
- 多步状态构造链:产品上架(厂商/合作伙伴)→ 加购 → 结算 → 订单,
  每级都是下一级的守卫

### G3 权限域:dashboard staff 门
- /dashboard/*:非 staff → 302(实测全 302 = 门真实生效)
- Partners/库存管理深块依赖 staff + partner 关联状态

### G4 DB 域:目录可见性
- product.is_public / stockrecord 库存门(分类浏览深块)

## 状态域覆盖
- SESSION:G1;DATABASE:G2(订单流)/G4(目录);RIGHTS:G3

## 接入门记录(2026-10-14)
- 门 1:24/24 non-5xx(302×17 = oscar 应用级重定向/门;200 admin;
  404 为 checkout 子步骤真实 URL 形态差异)
- 门 2:DATABASE_NAME env 每进程 sqlite ✓
- 门 3:见 gates 汇总表(后台批跑)
- venv:external/django-oscar/.venv(+sandbox 伴生包:django-environ/
  sorl-thumbnail/easy-thumbnails/django-haystack+Whoosh/whitenoise/
  widget-tweaks)
- 已知坑:catalogue.0017 迁移在 Django6/py3.13 确定性 sqlite 报错
  → --fake 跳过(纯字段属性变更,冒烟无感);ROOT_URLCONF='urls'
  为 sandbox 顶层模块,harness 双 sys.path 注入
