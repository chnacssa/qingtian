"""
config.py 单元测试
配置读取 + 环境变量覆盖
"""

import os

import pytest

from huanyu import config


class TestGetSchemaName:
    def test_default(self):
        assert config.get_schema_name() == "huanyu"


class TestGetRedisUrl:
    def test_default(self):
        assert "redis://" in config.get_redis_url()

    def test_env_var_override(self, monkeypatch):
        """QINGTIAN_REDIS_URL 环境变量优先（Docker compose 一键部署注入 redis://redis:6379）"""
        monkeypatch.setenv("QINGTIAN_REDIS_URL", "redis://redis:6379/0")
        assert config.get_redis_url() == "redis://redis:6379/0"

    def test_no_env_var_falls_back_to_config(self):
        os.environ.pop("QINGTIAN_REDIS_URL", None)
        assert config.get_redis_url().startswith("redis://")


class TestGetPeerId:
    def test_returns_string(self):
        assert isinstance(config.get_peer_id(), str)
        assert len(config.get_peer_id()) > 0


class TestGetMaxCounters:
    def test_default(self):
        assert config.get_max_counters() == 5


class TestGetNegotiationExpireDays:
    def test_default(self):
        assert config.get_negotiation_expire_days() == 7


class TestGetHeartbeatInterval:
    def test_default(self):
        assert config.get_heartbeat_interval() == 300

    def test_returns_int(self):
        assert isinstance(config.get_heartbeat_interval(), int)


class TestGetHeartbeatTimeout:
    def test_default(self):
        assert config.get_heartbeat_timeout() == 3


class TestMsgSignKey:
    def test_env_var_override(self, monkeypatch):
        monkeypatch.setenv("HUANYU_SIGN_KEY", "test-key-override")
        assert config.get_msg_sign_key() == "test-key-override"

    def test_no_env_var(self):
        # 确保环境变量不存在
        os.environ.pop("HUANYU_SIGN_KEY", None)
        key = config.get_msg_sign_key()
        # 返回配置值或空字符串
        assert isinstance(key, str)
