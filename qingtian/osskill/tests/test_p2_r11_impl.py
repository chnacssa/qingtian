"""osskill/implementations/ 层 R11 深度扫描 P2（中危）回归测试。

覆盖五项修复：
  1. _base.py       —— 路径占位符 + query 值 URL 编码（防特殊字符破坏 URL）
  2. portal/web.py  —— 上传大小钳制（防 file.read() 无界整读 OOM）
  3. portal/web.py  —— 删除归属校验不再受搜索窗口 100 条钳制误导
  4. portal/web.py  —— 共享文件不再被误判 owned 可删（精确比对 owner）
  5. portal/import_loader.py —— skills 目录路径配置化（QINGTIAN_SKILLS_DIR 兜底）

只测纯函数/助手与路由接线，不依赖真实服务（1996/5432）。
"""

import importlib
import tempfile
from pathlib import Path

import pytest

from osskill.implementations import _base
from osskill.implementations.portal import import_loader, web


# ── 测试桩 ──────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, status: int = 200, data: dict | None = None):
        self.status = status
        self._data = data if data is not None else {"ok": True}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, content_type=None):
        return self._data

    async def text(self):
        return ""


class _FakeSession:
    """记录实际发出的 URL，供断言编码结果。"""

    def __init__(self):
        self.calls: list[tuple] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def get(self, url, headers=None):
        self.calls.append(("GET", url, headers))
        return _FakeResponse()

    def request(self, method, url, json=None, headers=None):
        self.calls.append((method, url, headers))
        return _FakeResponse()

    def delete(self, url, headers=None):
        self.calls.append(("DELETE", url, headers))
        return _FakeResponse()


class _DemoSkill(_base.BaseProductSkill):
    name = "demo"
    display_name = "演示"
    description = "R11 P2 测试"
    category = "product"
    version = "0.1.0"

    CAPABILITIES = {
        "get_doc": {"method": "GET", "path": "/v1/product/documents/{id}"},
        "get_img": {"method": "GET", "path": "/v1/product/{product_id}/images/{img_id}"},
    }


class _FakeUploadFile:
    """按请求 size 产出字节的假上传文件（可模拟任意大小流而不真正分配大内存）。"""

    def __init__(self, size: int):
        self._size = size

    async def read(self, n: int = -1):
        want = self._size if n < 0 else min(n, self._size)
        return b"x" * want


# ── Fix 1: _base.py URL 编码 ───────────────────────────


class TestBaseUrlEncoding:
    def test_encode_path_encodes_special_chars(self):
        out = _base._encode_path("/v1/product/documents/{id}", {"id": "a b/c&d?e#f"})
        assert out == "/v1/product/documents/a%20b%2Fc%26d%3Fe%23f"

    def test_encode_path_keeps_literal_slashes(self):
        out = _base._encode_path(
            "/v1/product/{product_id}/images/{img_id}",
            {"product_id": "P/1", "img_id": "i"},
        )
        assert out == "/v1/product/P%2F1/images/i"

    def test_quote_query_val_encodes_query_break_chars(self):
        assert _base._quote_query_val("a&b=c d#e") == "a%26b%3Dc%20d%23e"

    @pytest.mark.asyncio
    async def test_call_api_encodes_path_and_query(self, monkeypatch):
        session = _FakeSession()
        monkeypatch.setattr(_base.aiohttp, "ClientSession", lambda *a, **k: session)
        skill = _DemoSkill()
        skill._agent_config = {"qingtian_url": "http://127.0.0.1:1996"}
        await skill._call_api(
            "GET", "/v1/product/documents/{id}",
            {"id": "a b/c", "enterprise_id": "e&x", "q": "x&y"},
        )
        _method, url, _headers = session.calls[0]
        assert "/v1/product/documents/a%20b%2Fc" in url
        assert "enterprise_id=e%26x" in url
        assert "q=x%26y" in url
        assert " " not in url and "&x&" not in url  # 无裸空格、无裸 & 破坏结构

    @pytest.mark.asyncio
    async def test_execute_encodes_multi_segment_params(self, monkeypatch):
        session = _FakeSession()
        monkeypatch.setattr(_base.aiohttp, "ClientSession", lambda *a, **k: session)
        skill = _DemoSkill()
        skill._agent_config = {"qingtian_url": "http://127.0.0.1:1996"}
        res = await skill.execute(
            {"action": "get_img", "product_id": "P/1", "img_id": "i?1"},
        )
        assert res["ok"] is True
        _method, url, _headers = session.calls[0]
        assert "/v1/product/P%2F1/images/i%3F1" in url

    @pytest.mark.asyncio
    async def test_execute_unknown_action_still_errors(self):
        res = await _DemoSkill().execute({"action": "nope"})
        assert res["ok"] is False


# ── Fix 2: portal/web.py 上传大小钳制 ───────────────────


class TestUploadBounded:
    @pytest.mark.asyncio
    async def test_upload_bounded_ok(self, monkeypatch):
        monkeypatch.setattr(web, "_MAX_UPLOAD_SIZE", 100)
        content = await web._read_upload_bounded(_FakeUploadFile(10))
        assert content == b"x" * 10

    @pytest.mark.asyncio
    async def test_upload_bounded_empty_400(self, monkeypatch):
        monkeypatch.setattr(web, "_MAX_UPLOAD_SIZE", 100)
        with pytest.raises(web.HTTPException) as ei:
            await web._read_upload_bounded(_FakeUploadFile(0))
        assert ei.value.status_code == 400

    @pytest.mark.asyncio
    async def test_upload_bounded_oversize_413(self, monkeypatch):
        monkeypatch.setattr(web, "_MAX_UPLOAD_SIZE", 10)
        with pytest.raises(web.HTTPException) as ei:
            await web._read_upload_bounded(_FakeUploadFile(11))
        assert ei.value.status_code == 413

    def test_max_upload_size_default_500mb(self):
        # 2026-08-31 波哥指示 300→500（客户投标文件超 200MB 常见）
        assert web._MAX_UPLOAD_SIZE == 500 * 1024 * 1024

    def test_max_upload_size_env_override(self, monkeypatch):
        monkeypatch.setenv("QINGTIAN_PORTAL_MAX_UPLOAD_MB", "1")
        reloaded = importlib.reload(web)
        try:
            assert reloaded._MAX_UPLOAD_SIZE == 1 * 1024 * 1024
        finally:
            monkeypatch.delenv("QINGTIAN_PORTAL_MAX_UPLOAD_MB")
            importlib.reload(web)


# ── Fix 3 & 4: portal/web.py 删除归属精确校验 ───────────


class TestIsFileOwned:
    @pytest.mark.asyncio
    async def test_owner_match_allowed(self, monkeypatch):
        async def fake_search(agent_id, query="", limit=100):
            return [{"file_id": "f1", "agent_id": "agent1"}]

        monkeypatch.setattr(web, "_huichuan_search", fake_search)
        assert await web._is_file_owned("agent1", "f1") is True

    @pytest.mark.asyncio
    async def test_shared_file_not_owned(self, monkeypatch):
        # 共享文件（owner 为他人）出现在搜索列表里 → 不再被误判 owned
        async def fake_search(agent_id, query="", limit=100):
            return [{"file_id": "f1", "agent_id": "other-agent"}]

        monkeypatch.setattr(web, "_huichuan_search", fake_search)
        assert await web._is_file_owned("agent1", "f1") is False

    @pytest.mark.asyncio
    async def test_empty_owner_denied(self, monkeypatch):
        async def fake_search(agent_id, query="", limit=100):
            return [{"file_id": "f1", "agent_id": ""}]

        monkeypatch.setattr(web, "_huichuan_search", fake_search)
        assert await web._is_file_owned("agent1", "f1") is False

    @pytest.mark.asyncio
    async def test_missing_file_fail_closed(self, monkeypatch):
        async def fake_search(agent_id, query="", limit=100):
            return [{"file_id": "other", "agent_id": "agent1"}]

        monkeypatch.setattr(web, "_huichuan_search", fake_search)
        assert await web._is_file_owned("agent1", "f1") is False

    @pytest.mark.asyncio
    async def test_search_uses_limit_100(self, monkeypatch):
        seen = {}

        async def fake_search(agent_id, query="", limit=100):
            seen["limit"] = limit
            return []

        monkeypatch.setattr(web, "_huichuan_search", fake_search)
        await web._is_file_owned("agent1", "f1")
        assert seen["limit"] == 100


class TestPortalDeleteRoute:
    @pytest.mark.asyncio
    async def test_delete_owned_allowed(self, monkeypatch):
        async def fake_owned(*a, **k):
            return True

        async def fake_delete(*a, **k):
            return True

        monkeypatch.setattr(web, "_is_file_owned", fake_owned)
        monkeypatch.setattr(web, "_huichuan_delete", fake_delete)
        res = await web.portal_delete_file("f1", agent_id="agent1")
        assert res["ok"] is True and res["file_id"] == "f1"

    @pytest.mark.asyncio
    async def test_delete_not_owned_403(self, monkeypatch):
        async def fake_owned(*a, **k):
            return False

        monkeypatch.setattr(web, "_is_file_owned", fake_owned)
        with pytest.raises(web.HTTPException) as ei:
            await web.portal_delete_file("f1", agent_id="agent1")
        assert ei.value.status_code == 403


# ── Fix 5: portal/import_loader.py 路径配置化 ───────────


class TestImportLoaderPath:
    def test_skills_root_default(self, monkeypatch):
        monkeypatch.delenv("QINGTIAN_SKILLS_DIR", raising=False)
        assert import_loader._skills_root() == Path("/opt/qingtian/skills")

    def test_skills_root_env_override(self, monkeypatch):
        monkeypatch.setenv("QINGTIAN_SKILLS_DIR", "/tmp/custom-skills")
        assert import_loader._skills_root() == Path("/tmp/custom-skills")

    def test_load_build_template_from_configured_root(self, monkeypatch):
        with tempfile.TemporaryDirectory() as tmp:
            mod_dir = Path(tmp) / "skills" / "sales"
            mod_dir.mkdir(parents=True)
            (mod_dir / "import_export.py").write_text(
                "def build_template(kind, tag=''):\n"
                "    return f'xlsx-{kind}-{tag}'\n",
                encoding="utf-8",
            )
            monkeypatch.setenv("QINGTIAN_SKILLS_DIR", str(Path(tmp) / "skills"))
            import_loader._load_import_export.cache_clear()
            try:
                build_template = import_loader.load_build_template("sales")
                assert callable(build_template)
                assert build_template("products") == "xlsx-products-"
            finally:
                import_loader._load_import_export.cache_clear()

    def test_load_build_template_missing_raises(self, monkeypatch):
        with tempfile.TemporaryDirectory() as tmp:
            monkeypatch.setenv("QINGTIAN_SKILLS_DIR", tmp)
            import_loader._load_import_export.cache_clear()
            try:
                with pytest.raises(ImportError):
                    import_loader.load_build_template("sales")
            finally:
                import_loader._load_import_export.cache_clear()
