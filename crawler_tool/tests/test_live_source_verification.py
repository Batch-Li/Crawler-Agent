import os

import pytest

from crawler_tool.validation import (
    ValidationConfig,
    run_south_weekend_pagination_evidence,
    run_toutiao_two_page_smoke,
)


pytestmark = pytest.mark.real_source_validation

if not (
    os.getenv("CRAWLER_LIVE_SMOKE") == "1"
    and os.getenv("CRAWLER_CONFIRM_REAL") == "1"
):
    pytest.skip("real source validation requires both explicit environment gates", allow_module_level=True)


def test_toutiao_two_page_real_smoke():
    report, code = run_toutiao_two_page_smoke(
        ValidationConfig(query="AI客服", allow_network=True, confirm_real=True)
    )
    assert code == 0, report


def test_south_weekend_pagination_real_evidence():
    report, code = run_south_weekend_pagination_evidence(
        ValidationConfig(query="AI客服", allow_network=True, confirm_real=True)
    )
    assert code in {0, 3, 4}
    assert report["verification"] == "south_weekend_pagination_evidence"
