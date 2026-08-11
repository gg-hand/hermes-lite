"""验证 FastAPI OpenAPI 文档聚合。"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_openapi_docs_accessible():
    """/docs 端点可访问。"""
    app = FastAPI(title="Test API")
    with TestClient(app) as client:
        resp = client.get("/docs")
        assert resp.status_code == 200


def test_openapi_json_accessible():
    """/openapi.json 端点可访问。"""
    app = FastAPI(title="Test API")
    with TestClient(app) as client:
        resp = client.get("/openapi.json")
        assert resp.status_code == 200
        data = resp.json()
        assert data["openapi"].startswith("3.")
