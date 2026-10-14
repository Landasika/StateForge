"""wger 靶 harness(健身管理,Django 6.0 + sqlite,批 2)。

启动:settings.local_dev(DEBUG=True,sqlite,USE_CELERY=False)
→ 每进程独立 sqlite(BOOT 后覆写 settings.DATABASES["default"]["NAME"])
→ migrate → loaddata(languages/licenses/单位)→ ORM 引导
  (superuser + GymConfig(pk=1) + exercise/routine/nutrition plan 种子)
→ django.test.Client(会话 cookie 跨请求持久 = 跨请求状态载体)。

坑位记录:
- manage.py 默认 settings.main 要求 DJANGO_DB_ENGINE 等 env 且面向 postgres;
  local_dev 自带 sqlite(django_prometheus 引擎)+ ALLOWED_HOSTS=['*'],直接用;
- wger/__init__.py 无条件 from wger.celery_configuration import app → venv 缺
  celery 时 django.setup() 即 ImportError,须 pip install celery[redis] flower;
- migrate 不种基础数据:Language 空 → 建 user 时 UserProfile 信号 FK 炸
  (IntegrityError),须先 loaddata languages licenses setting_*_units;
- 模板 context processor 硬编码 GymConfig.objects.get(pk=1)(wger/utils/
  context_processor.py:106),无行则全站模板 500,须显式建 pk=1;
- URL 全部挂 i18n_patterns 前缀(如 /en/...),路由表带 /en 前缀。

状态语义(NOTES.md 详):
- 会话域:WgerAuthenticationMiddleware 登录态 + routine/weight 等受保护页;
- DB 域:Routine 存在性与 owner(view/edit/logs 按 <int:pk> 定位)、
  NutritionPlan 按 <uuid:id> 定位、guest 用户按路径即时建号。
"""

from __future__ import annotations

import datetime
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[4] / "external" / "wger"

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "settings.local_dev")
_DB = Path(os.environ.get("SF_DB", REPO / f".sf_db_{os.getpid()}.sqlite3"))

sys.path.insert(0, str(REPO))

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402

settings.DATABASES["default"]["NAME"] = str(_DB)
settings.ALLOWED_HOSTS = list(settings.ALLOWED_HOSTS) + ["testserver", "localhost"]

from django.core.management import call_command  # noqa: E402

call_command("migrate", verbosity=0, interactive=False, run_syncdb=True)
for _fixture in ("languages", "licenses", "setting_repetition_units", "setting_weight_units"):
    call_command("loaddata", _fixture, verbosity=0)

# ---- 引导:superuser + GymConfig + 训练/营养/动作种子 ----
from django.contrib.auth import get_user_model  # noqa: E402

_USER, _PASS = "probe", "Passw0rd!"
User = get_user_model()
if not User.objects.filter(username=_USER).exists():
    _u = User(username=_USER, email="probe@wger.local", is_superuser=True, is_staff=True)
    _u.set_password(_PASS)
    _u.save()
user = User.objects.get(username=_USER)

from wger.config.models import GymConfig  # noqa: E402

if not GymConfig.objects.filter(pk=1).exists():
    GymConfig.objects.create(pk=1)

from wger.core.models import Language  # noqa: E402
from wger.exercises.models import Exercise, ExerciseCategory, Translation  # noqa: E402

_lang = Language.objects.get(short_name="en")
_cat = ExerciseCategory.objects.first()
if _cat is None:
    _cat = ExerciseCategory.objects.create(name="Probe Category")
_exercise = Exercise.objects.filter(category=_cat).first()
if _exercise is None:
    _exercise = Exercise.objects.create(category=_cat)
_translation = Translation.objects.filter(exercise=_exercise).first()
if _translation is None:
    _translation = Translation.objects.create(
        exercise=_exercise,
        name="Probe Exercise",
        description_source="Probe exercise description " + "x" * 45,
        language=_lang,
    )

from wger.manager.models import Routine  # noqa: E402

_today = datetime.date.today()
_routine = Routine.objects.filter(user=user).first()
if _routine is None:
    _routine = Routine(
        user=user,
        name="Probe Routine",
        description="probe",
        start=_today,
        end=_today + datetime.timedelta(days=30),
    )
    _routine.save()

from wger.nutrition.models import NutritionPlan  # noqa: E402

_plan = NutritionPlan.objects.filter(user=user).first()
if _plan is None:
    _plan = NutritionPlan(user=user, description="Probe plan")
    _plan.save()

from django.test import Client  # noqa: E402

CLIENT = Client()
CLIENT.login(username=_USER, password=_PASS)

ROUTINE_ID = _routine.pk
PLAN_ID = _plan.pk
EXERCISE_ID = _exercise.pk
USER_ID = user.pk

# ---- 路由表(冒烟 + 种子参数池共用;第三列标注状态域;i18n /en 前缀) ----
ROUTES: list[tuple[str, str, str]] = [
    ("GET", "/en/", "public"),                          # index:按登录态 302 dashboard
    ("GET", "/en/user/login", "public"),
    ("GET", "/en/user/registration", "public"),
    ("GET", "/account/login/", "public"),               # allauth 登录
    # 会话域:登录后个人/训练/营养页面
    ("GET", "/en/dashboard", "session"),
    ("GET", "/en/user/preferences", "session"),
    ("GET", "/en/user/api-key", "session"),
    ("GET", "/en/user/demo-entries", "session"),
    ("GET", "/en/user/list", "session"),
    ("GET", f"/en/user/{USER_ID}/overview", "session+db"),   # 用户存在性 + 权限
    ("GET", "/en/routine/overview", "session"),
    ("GET", "/en/routine/add", "session"),
    ("GET", f"/en/routine/{ROUTINE_ID}/view", "session+db"),  # Routine 存在性
    ("GET", f"/en/routine/{ROUTINE_ID}/edit", "session+db"),
    ("GET", f"/en/routine/{ROUTINE_ID}/logs", "session+db"),
    ("GET", f"/en/routine/{ROUTINE_ID}/table", "session+db"),
    ("GET", f"/en/routine/{ROUTINE_ID}/statistics", "session+db"),
    ("GET", "/en/routine/calendar", "session"),
    ("GET", "/en/routine/templates/overview/private", "session"),
    ("GET", "/en/routine/templates/overview/public", "public"),
    ("GET", "/en/weight/overview", "session"),
    ("GET", "/en/weight/export-csv/", "session"),
    ("GET", "/en/weight/import-csv/", "session"),
    ("GET", "/en/nutrition/overview/", "session"),
    ("GET", f"/en/nutrition/{PLAN_ID}/view/", "session+db"),  # NutritionPlan 按 uuid 定位
    ("GET", f"/en/nutrition/{PLAN_ID}/pdf", "session+db"),
    ("GET", "/en/nutrition/ingredient/overview/", "public"),
    ("GET", "/en/nutrition/calculator/bmi/", "public"),
    ("GET", "/en/exercise/overview/", "public"),
    ("GET", f"/en/exercise/{EXERCISE_ID}/view", "public+db"),
    ("GET", "/en/software/about-us", "public"),
    ("GET", "/en/software/features", "public"),
    ("GET", "/en/software/api", "public"),
    ("GET", "/en/language/list", "public"),
    ("GET", "/sitemap.xml", "public"),
    ("GET", "/robots.txt", "public"),
    # API v2:DRF 门(JWT/session);未带令牌也 non-5xx
    ("GET", "/api/v2/", "api-auth"),
    ("GET", "/api/v2/routine/", "api-auth"),
    ("GET", "/api/v2/exercise/", "api-auth"),
    ("GET", "/en/user/logout", "session"),              # 放最后:GET 注销会话
]


def smoke() -> list[tuple[str, int]]:
    """门 1:路由表冒烟,返回 [(route, status)]。"""
    out = []
    for method, path, auth in ROUTES:
        try:
            r = CLIENT.get(path) if method == "GET" else CLIENT.post(path)
            out.append((f"{method} {path}", r.status_code))
        except Exception as exc:                 # noqa: BLE001
            out.append((f"{method} {path}", f"EXC:{type(exc).__name__}"))
    return out


if __name__ == "__main__":
    results = smoke()
    ok = sum(1 for _, s in results if isinstance(s, int) and s < 500)
    print(f"smoke: {ok}/{len(results)} non-5xx")
    for route, status in results:
        print(f"  {status!s:>4}  {route}")
