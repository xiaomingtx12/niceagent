"""alembic 迁移链静态验证。

不依赖真实 DB 连接 — 解析 internal/migration/versions/ 下所有
d_v2_*.py 文件的 revision/down_revision，验证:
- 每个 down_revision 指向的 rev 必须存在 (master 老迁移除外, 在不在
  d_v2_ 命名空间视为正常 merge head)
- 单 head: 链收束到唯一一个末端
- 每个 d_v2 迁移文件都解析成功 (revision 字符串合法)

为什么离线校验:
- master alembic 配置走 Flask-Migrate 模式, 没硬编码 script_location,
  本地无法 alembic upgrade head
- 真实链验证留给 CI / 生产 alembic upgrade; 这里只保证 d_v2_*.py 之间
  不自相矛盾 (一个人写错 down_revision 立即被这测试抓住)
"""
from pathlib import Path

import pytest

VERSIONS_DIR = Path("internal/migration/versions")
PREFIX = "d_v2_"


def _parse_revisions():
    """解析所有 d_v2_*.py 文件的 (revision, down_revision, filename)。"""
    import re

    files = sorted(VERSIONS_DIR.glob(f"{PREFIX}*.py"))
    parsed = []
    for f in files:
        text = f.read_text(encoding="utf-8")
        m_rev = re.search(
            r"""^revision\s*=\s*['"]([^'"]+)['"]""", text, re.M
        )
        m_down = re.search(
            r"""^down_revision\s*=\s*(.+?)\n""", text, re.M
        )
        if not m_rev or not m_down:
            pytest.fail(
                f"{f.name}: missing revision or down_revision"
            )
        rev = m_rev.group(1)
        down_raw = m_down.group(1).strip()
        if down_raw.startswith("("):
            down = tuple(re.findall(r"""['"]([^'"]+)['"]""", down_raw))
        else:
            m = re.search(r"""['"]([^'"]+)['"]""", down_raw)
            down = m.group(1) if m else None
        parsed.append((rev, down, f.name))
    return parsed


def test_d_v2_chain_resolves():
    """所有 d_v2 迁移 down_revision 都指向已存在的 rev (master 视为合法 head)。"""
    parsed = _parse_revisions()
    revs = {r for r, _, _ in parsed}
    for rev, down, fname in parsed:
        if down is None:
            continue
        if isinstance(down, tuple):
            for d in down:
                if d not in revs and _is_master_head(d):
                    continue
                assert d in revs, (
                    f"{fname}: down_revision {d!r} not found in d_v2 chain"
                )
        else:
            if down not in revs and _is_master_head(down):
                continue
            assert down in revs, (
                f"{fname}: down_revision {down!r} not found in d_v2 chain"
            )


def test_d_v2_single_head():
    """d_v2 链只允许 1 个 head (末端)。"""
    parsed = _parse_revisions()
    revs = {r for r, _, _ in parsed}
    incoming = set()
    for _, down, _ in parsed:
        if down is None:
            continue
        if isinstance(down, tuple):
            incoming.update(down)
        else:
            incoming.add(down)
    heads = revs - incoming
    assert len(heads) == 1, (
        f"expected 1 head, got {len(heads)}: {sorted(heads)}"
    )


def test_d_v2_003_is_head():
    """R3 memory_facts 是当前末端 (R3 后唯一新表)."""
    parsed = _parse_revisions()
    revs = {r for r, _, _ in parsed}
    incoming = set()
    for _, down, _ in parsed:
        if down is None:
            continue
        if isinstance(down, tuple):
            incoming.update(down)
        else:
            incoming.add(down)
    heads = revs - incoming
    assert "d_v2_003_create_memory_facts" in heads


def _is_master_head(rev: str) -> bool:
    """master 历史 head (R0 之前) 视为合法 merge target。"""
    return rev in {
        "223e8bdd31a6",  # master 老 chain
        "6837222596d1",
        "fe593d5bb1ab",
    }