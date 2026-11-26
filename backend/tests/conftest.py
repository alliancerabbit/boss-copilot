"""测试夹具：把 backend/ 加进 sys.path，并提供绑定回环地址的 TestClient。"""

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import main  # noqa: E402  （必须在 sys.path 调整之后导入）


@pytest.fixture()
def client() -> TestClient:
    # base_url 必须是回环地址，否则会被 Host 校验拦成 400
    return TestClient(main.app, base_url="http://127.0.0.1:8000")
