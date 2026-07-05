import ast
from pathlib import Path


def _literal_assignment(tree: ast.Module, name: str):
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == name:
                return ast.literal_eval(node.value)
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.literal_eval(node.value)
    raise AssertionError(f"{name} не найден")


def test_alembic_migrations_are_single_linear_chain():
    revisions: dict[str, str | None] = {}
    for path in sorted(Path("alembic/versions").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        revision = _literal_assignment(tree, "revision")
        down_revision = _literal_assignment(tree, "down_revision")
        assert revision not in revisions, f"дублируется revision {revision}"
        revisions[revision] = down_revision

    assert revisions
    roots = [rev for rev, down in revisions.items() if down is None]
    assert roots == ["0001_initial"]

    for rev, down in revisions.items():
        if down is not None:
            assert down in revisions, f"{rev} ссылается на отсутствующую миграцию {down}"

    referenced = {down for down in revisions.values() if down is not None}
    heads = sorted(set(revisions) - referenced)
    assert heads == ["0015_rc_geofence_radius"]

    walked = []
    current = heads[0]
    while current is not None:
        walked.append(current)
        current = revisions[current]
    assert len(walked) == len(revisions)
