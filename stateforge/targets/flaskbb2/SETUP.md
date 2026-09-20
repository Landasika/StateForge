# FlaskBB 2.1 运行手册(2026-09-20 实装通过)

> 位置:源码 `stateforge/targets/flaskbb2`(v2.1.0);独立 venv `~/venvs/flaskbb`(**由 ANOTA 解释器创建**——
> venv 内 python 仍带 ANOTA_EXECUTION,将来可挂注解);配置 `/tmp/flaskbb.cfg`;数据库 `/tmp/flaskbb.db`。
> 主 ANOTA 环境(flask 2.2.2/SA 2.0.2 锁定栈)零污染——venv 隔离。

## 启动三步

```bash
# 0) 杀旧进程(按端口,勿用 pkill 模式——会自杀)
fuser -k 5080/tcp 2>/dev/null; sleep 2
# 1) 起服务(注意 SERVER_NAME 已改为 127.0.0.1:5080,不匹配会全部 404!)
(~/venvs/flaskbb/bin/flaskbb --config /tmp/flaskbb.cfg run --port 5080 &)
sleep 9 && curl http://127.0.0.1:5080/        # 200 + FlaskBB 首页
# 管理员:admin / admin123secure(组:admin)
```

重建(从零):`flaskbb makeconfig -d` → 改 `SQLALCHEMY_DATABASE_URI=sqlite:////tmp/flaskbb.db` 与
`SERVER_NAME="127.0.0.1:5080"` → `flaskbb db upgrade`(populate 不建表,须先走 alembic)→
`flaskbb populate` → `flaskbb users new -u admin -e ... -p ... -g admin`。

## 装环境踩过的坑(全部已解决,重建时按此绕行)

1. **celery 5.0-5.2 的 wheel 元数据非法**,现代 pip 拒装且拖垮整份 requirements 解析 →
   装 `celery==5.6.3`(不带旧钉子)——它需要 click 8,FlaskBB CLI 兼容
2. `flask-allows`:PyPI 0.7.1 用 Flask-1 私有 API(`flask._compat`),git master 又要 py≥3.12 →
   **装 PyPI 0.7.1 + 手写垫片** `venv/.../flask/_compat.py`(with_metaclass,6 行)
3. requirements 自引用 FlaskBB/插件链会拖坏解析 → `pip install --no-deps -r`(2021 锁文件本身
   齐全,除 celery 行),最后补 `WTForms WTForms-SQLAlchemy`
4. `setup.py` 的 flask-allows git 直链(github 被墙)→ 已打补丁为 `flask-allows`
5. `SERVER_NAME='localhost:5000'` 与实际端口不符 → Flask 对所有请求 404(路由其实在)

## 与分析/靶场的关系

- 分析(已完成):`python3 -m stateforge.cli analyze stateforge/targets/flaskbb2 --framework flask`
  → 147 端点/调用图 206 节点(注册式路由已支持)
- **当 fuzz 靶**:尚需 ①注解变体(venv 内 python 是 ANOTA 的——技术上可行)②supervisor 接入
  ③快照 L2(db 在 /tmp/flaskbb.db,文件模板即可)。未列入近期任务,PyGoat 仍是第四靶。
