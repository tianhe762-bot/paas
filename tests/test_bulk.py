import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.mark.skip(reason="用户要求：不再自动运行几千条批量测试（需要时可手动执行 scripts/bulk_test.py）")
def test_bulk_cases(tmp_path):
    from scripts.bulk_test import run_bulk

    cases, failures = run_bulk(tmp_path / "bulk.db", tmp_path / "secret.key")
    assert len(cases) > 1000, f"用例数不足: {len(cases)}"
    assert failures == [], f"{len(failures)} 个用例失败: {failures[:5]}"
