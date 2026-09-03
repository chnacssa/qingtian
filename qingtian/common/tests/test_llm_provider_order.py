# -*- coding: utf-8 -*-
"""全局 FIRST_LLM/SECOND_LLM 厂商顺序（波哥 2026-08-27 定调）回归测试：

一处一个硬编码模型名的时代结束——底座所有 LLM 默认值统一走
common/config.py 的档案解析（_LLM_PROVIDER_PROFILES + _active_llm_profile），
common/llm、xixing、yongheng、huichuan、zhice、setup 全部引用。

读取顺序：FIRST_LLM（指定厂商且配了对应 key）→ SECOND_LLM 兜底 →
都没配时智谱优先自动推导。模型/端点/key 三者同档案（不会拿 A 家 key 打 B 家端点）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import pytest

from common.config import (
    default_llm_model,
    default_llm_base_url,
    default_llm_provider,
    default_llm_key_var,
    default_llm_backup_profile,
)


@pytest.fixture
def clean_env(monkeypatch):
    for var in ("FIRST_LLM", "SECOND_LLM", "ZHIPU_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(var, raising=False)


def test_no_keys_defaults_zhipu(clean_env):
    """无任何 key → 智谱默认口径（报缺 key 时按智谱）。"""
    assert default_llm_model() == "glm-5.3-flash"
    assert default_llm_provider() == "zhipu"
    assert default_llm_base_url() == "https://open.bigmodel.cn/api/paas/v4"
    assert default_llm_key_var() == "ZHIPU_API_KEY"


def test_auto_detect_deepseek_only(clean_env, monkeypatch):
    """仅 DEEPSEEK_API_KEY → deepseek 档案（防拿 ds key 打智谱端点）。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds")
    assert default_llm_model() == "deepseek-v4-flash"
    assert default_llm_base_url() == "https://api.deepseek.com/v1"
    assert default_llm_key_var() == "DEEPSEEK_API_KEY"
    assert default_llm_backup_profile() is None  # 另一家无 key，无备用


def test_both_keys_zhipu_first(clean_env, monkeypatch):
    """两家 key 都在 → 智谱优先，deepseek 备用。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds")
    monkeypatch.setenv("ZHIPU_API_KEY", "zp")
    assert default_llm_model() == "glm-5.3-flash"
    assert default_llm_backup_profile()["model"] == "deepseek-v4-flash"


def test_first_llm_overrides_default_order(clean_env, monkeypatch):
    """FIRST_LLM=deepseek 反转默认序（大小写不敏感），备用落另一家。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds")
    monkeypatch.setenv("ZHIPU_API_KEY", "zp")
    monkeypatch.setenv("FIRST_LLM", "DeepSeek")
    assert default_llm_model() == "deepseek-v4-flash"
    assert default_llm_provider() == "deepseek"
    assert default_llm_backup_profile()["model"] == "glm-5.3-flash"


def test_first_llm_without_key_falls_to_second(clean_env, monkeypatch):
    """FIRST 指定厂商没配 key → SECOND_LLM 兜底。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds")
    monkeypatch.setenv("FIRST_LLM", "zhipu")      # 无 ZHIPU key
    monkeypatch.setenv("SECOND_LLM", "DEEPSEEK")  # 大小写不敏感
    assert default_llm_model() == "deepseek-v4-flash"
    assert default_llm_provider() == "deepseek"


def test_second_llm_equal_active_falls_to_other(clean_env, monkeypatch):
    """SECOND 与主厂商相同 → 备用取另一家（不自己备自己）。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds")
    monkeypatch.setenv("ZHIPU_API_KEY", "zp")
    monkeypatch.setenv("FIRST_LLM", "deepseek")
    monkeypatch.setenv("SECOND_LLM", "deepseek")
    assert default_llm_model() == "deepseek-v4-flash"
    assert default_llm_backup_profile()["model"] == "glm-5.3-flash"


def test_common_llm_config_follows_profile(clean_env, monkeypatch):
    """common/llm.get_llm_config 全局默认随档案（模型+端点+key 三同源）。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds")
    monkeypatch.setenv("ZHIPU_API_KEY", "zp")
    monkeypatch.setenv("FIRST_LLM", "deepseek")
    from common import llm as llm_mod
    cfg = llm_mod.get_llm_config()
    assert cfg.model == "deepseek-v4-flash"
    assert cfg.provider == "deepseek"
    assert cfg.base_url == "https://api.deepseek.com/v1"
    assert cfg.api_key == "ds"
    backup = llm_mod.get_backup_config()
    assert backup is not None and backup.model == "glm-5.3-flash"
    assert backup.api_key == "zp"


def test_module_configs_follow_profile(clean_env, monkeypatch):
    """xixing/yongheng/huichuan/zhice 各 config 默认统一随档案（消灭一处一个硬编码）。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds")
    monkeypatch.setenv("ZHIPU_API_KEY", "zp")
    monkeypatch.setenv("FIRST_LLM", "deepseek")
    from xixing import config as xixing_cfg
    from yongheng import config as yongheng_cfg
    from huichuan import config as huichuan_cfg
    from zhice import config as zhice_cfg
    assert xixing_cfg.get_classifier_model() == "deepseek-v4-flash"
    assert xixing_cfg.get_distiller_model() == "deepseek-v4-flash"
    assert xixing_cfg.get_proposal_model() == "deepseek-v4-flash"
    assert xixing_cfg.get_quality_judge_model() == "deepseek-v4-flash"
    assert xixing_cfg.get_deepseek_key() == "ds"
    assert xixing_cfg.get_deepseek_base_url() == "https://api.deepseek.com/v1"
    assert yongheng_cfg.get_llm_high_value_model() == "deepseek-v4-flash"
    assert yongheng_cfg.get_llm_digest_model() == "deepseek-v4-flash"
    assert yongheng_cfg.get_llm_agentic_model() == "deepseek-v4-flash"
    assert yongheng_cfg.get_llm_base_url() == "https://api.deepseek.com/v1"
    assert yongheng_cfg.get_llm_api_key() == "ds"
    assert huichuan_cfg.get_refine_llm_model() == "deepseek-v4-flash"
    assert zhice_cfg.get_llm_decompose_model() == "deepseek-v4-flash"
    assert zhice_cfg.get_llm_base_url() == "https://api.deepseek.com/v1"
    assert zhice_cfg.get_llm_api_key() == "ds"
