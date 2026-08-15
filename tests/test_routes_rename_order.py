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
    src = _source("app/web/router.py")
    start = src.index("async def routes_page")
    body = src[start:start + 4000]
    assert "RouteTemplate.sort_order" in body, "список должен сортироваться по порядку владельца"


def test_no_browser_confirm_left_in_templates():
    """Подтверждения удаления — своим окном, а не серым браузерным."""
    for path in Path("app/web/templates").glob("*.html"):
        text = path.read_text(encoding="utf-8")
        assert "return confirm(" not in text, f"{path.name}: остался браузерный confirm()"
