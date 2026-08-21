"""Регресс: переименование РЦ и порядок маршрутов (правки 15.08.2026).

Правило, которое здесь закрепляется:
  - переименовали РЦ  → ШАБЛОНЫ маршрутов получают новое имя (водитель в боте
    должен видеть актуальное название, иначе путает три склада одной сети);
  - РЕЙСЫ не трогаем никогда — это исторические факты, по ним печатаются акты
    для налоговой.
"""
import ast
from pathlib import Path


def _source(name: str) -> str:
    return Path(name).read_text(encoding="utf-8")


def test_rename_updates_templates_but_not_trips():
    src = _source("app/web/router.py")
    start = src.index("async def routes_rc_edit")
    body = src[start:start + 3000]
    # шаблоны переименовываются
    assert "update(RouteTemplate)" in body
    # рейсы — нет
    assert "update(Trip)" not in body, "переименование не должно трогать рейсы"


def test_route_template_has_sort_order_field():
    tree = ast.parse(_source("app/models.py"))
    cls = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.ClassDef) and n.name == "RouteTemplate"
    )
    fields = {
        t.target.id for t in cls.body if isinstance(t, ast.AnnAssign)
    }
    assert "sort_order" in fields


def test_templates_ordered_by_sort_order():
    """И сайт, и бот показывают маршруты в порядке, заданном владельцем."""
    src = _source("app/web/router.py")
    start = src.index("async def _route_templates_view")
    body = src[start:start + 2500]
    assert "RouteTemplate.sort_order" in body, "список должен сортироваться по порядку владельца"
    # бот обязан сортировать так же, иначе порядок не доедет до водителя
    bot = _source("app/bots/driver_bot.py")
    assert bot.count("RouteTemplate.sort_order") >= 2, "обе выборки в боте должны учитывать порядок"


def test_no_browser_confirm_left_in_templates():
    """Подтверждения удаления — своим окном, а не серым браузерным."""
    for path in Path("app/web/templates").glob("*.html"):
        text = path.read_text(encoding="utf-8")
        assert "return confirm(" not in text, f"{path.name}: остался браузерный confirm()"


def test_move_returns_partial_not_redirect():
    """Перестановка ▲▼ обновляет только список (HTMX), без перезагрузки страницы."""
    src = _source("app/web/router.py")
    start = src.index("async def routes_template_move")
    end = src.index('@app.post("/routes/rc/add")')
    body = src[start:end]
    assert "_route_templates.html" in body, "должен отдаваться кусок списка"
    assert "_is_htmx" in body
    # прямых редиректов на /routes в теле остаться не должно
    assert 'RedirectResponse("/routes", status_code=303)' in body, "фоллбэк без htmx нужен"
    assert body.count("RedirectResponse") == 1, "остальные ответы — партиал"


def test_partial_uses_htmx_and_marks_stale():
    tpl = _source("app/web/templates/_route_templates.html")
    assert 'id="route-templates"' in tpl
    assert 'hx-target="#route-templates"' in tpl
    assert "stale_destinations" in tpl, "устаревшие названия РЦ должны подсвечиваться"

