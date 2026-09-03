#!/usr/bin/env python3
"""生成开发者自签名证书 — Phase 1 开发环境使用

用法:
    python scripts/generate-dev-certs.py

自动完成:
    1. 生成 Ed25519 开发密钥对（如已存在则跳过）
    2. 写入 dev_platform_pubkey.hex（供 loader.py 加载）
    3. 扫描 osskill/implementations/ 下所有 skill.json
    4. 为每个 skill.json 生成自签名证书，写入 certificate 字段

注意:
    开发证书有效期 10 年，仅限开发/测试环境。
    生产环境由 CI/CD 注入平台密钥 + acssa.cn 签发证书覆盖。
"""

import json
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives import serialization

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent
IMPL_DIR = PROJECT_ROOT / "osskill" / "implementations"
SCRIPTS_DIR = PROJECT_ROOT / "scripts"

# 开发密钥路径
DEV_KEY_FILE = SCRIPTS_DIR / "dev_ed25519_privkey.hex"
DEV_PUBKEY_FILE = SCRIPTS_DIR / "dev_platform_pubkey.hex"

# 开发证书有效期（Unix 时间戳）
# 2026-01-01 ~ 2036-01-01
NOT_BEFORE = 1767225600
NOT_AFTER = 2082844800


def _ensure_dev_keypair():
    """如果不存在，生成 Ed25519 开发密钥对"""
    key_exists = DEV_KEY_FILE.exists()
    pubkey_exists = DEV_PUBKEY_FILE.exists()

    if key_exists and pubkey_exists:
        print("[OK] 开发密钥对已存在，跳过生成")
        priv_hex = DEV_KEY_FILE.read_text("utf-8").strip()
        return priv_hex

    if key_exists and not pubkey_exists:
        print("[WARN] 私钥文件存在但公钥文件缺失，将重新生成密钥对")
    elif pubkey_exists and not key_exists:
        print("[WARN] 公钥文件存在但私钥文件缺失，将重新生成密钥对")

    private_key = ed25519.Ed25519PrivateKey.generate()
    public_key = private_key.public_key()

    # 导出私钥 hex
    priv_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    priv_hex = priv_bytes.hex()

    # 导出公钥 hex
    pub_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    pub_hex = pub_bytes.hex()

    DEV_KEY_FILE.write_text(priv_hex + "\n", "utf-8")
    DEV_PUBKEY_FILE.write_text(pub_hex + "\n", "utf-8")

    print(f"[OK] 生成开发密钥对:")
    print(f"    私钥: {DEV_KEY_FILE}")
    print(f"    公钥: {DEV_PUBKEY_FILE}")
    print(f"    公钥 hex: {pub_hex}")
    return priv_hex


def _generate_cert(skill_name: str, priv_hex: str) -> str:
    """生成自签名证书 hex

    证书结构：[64 字节 Ed25519 签名] + [JSON 载荷]
    载荷: {"skill": name, "not_before": ts, "not_after": ts}
    """
    priv_bytes = bytes.fromhex(priv_hex)
    private_key = ed25519.Ed25519PrivateKey.from_private_bytes(priv_bytes)

    payload = json.dumps({
        "skill": skill_name,
        "not_before": NOT_BEFORE,
        "not_after": NOT_AFTER,
    }).encode("utf-8")

    signature = private_key.sign(payload)
    cert_bytes = signature + payload
    return cert_bytes.hex()


def _scan_skills():
    """扫描所有 skill.json，返回 (skill_name, filepath) 列表"""
    results = []
    if not IMPL_DIR.exists():
        print(f"[!] implementations 目录不存在: {IMPL_DIR}")
        return results

    for entry in sorted(IMPL_DIR.iterdir()):
        if not entry.is_dir():
            continue
        skill_json = entry / "skill.json"
        if skill_json.exists():
            try:
                with open(skill_json, "r", encoding="utf-8") as f:
                    data = json.load(f)
                name = data.get("name", "")
                if name:
                    results.append((name, skill_json))
                else:
                    print(f"  [!] {skill_json}: name 字段为空，跳过")
            except (json.JSONDecodeError, OSError) as e:
                print(f"  [!] {skill_json}: 读取失败 - {e}")

    return results


def main():
    print("=" * 60)
    print("  开发者自签名证书生成工具")
    print("=" * 60)

    # Step 1: 确保密钥对
    priv_hex = _ensure_dev_keypair()
    pub_hex = DEV_PUBKEY_FILE.read_text("utf-8").strip()

    print(f"\n[1/3] 公钥: {pub_hex[:16]}...{pub_hex[-16:]}")

    # Step 2: 扫描所有 skill.json
    skills = _scan_skills()
    print(f"\n[2/3] 扫描到 {len(skills)} 个 skill.json")

    # Step 3: 生成并写入证书
    signed = 0
    skipped = 0
    errors = 0

    for name, path in skills:
        try:
            cert_hex = _generate_cert(name, priv_hex)
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            old_cert = data.get("certificate", "")
            data["certificate"] = cert_hex

            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.write("\n")

            cert_preview = cert_hex[:16]
            if old_cert and old_cert != cert_hex:
                print(f"  [UPD] {name}: 证书已更新 ({cert_preview}...)")
            elif old_cert == cert_hex:
                print(f"  [--] {name}: 证书未变化，跳过")
                skipped += 1
                continue
            else:
                print(f"  [++] {name}: 证书已写入 ({cert_preview}...)")
            signed += 1
        except Exception as e:
            print(f"  [ERR] {name}: 签名失败 - {e}")
            errors += 1

    # 汇总
    print(f"\n{'=' * 60}")
    print(f"  摘要: {signed} 已签名, {skipped} 跳过, {errors} 错误")
    print(f"\n  公钥文件: {DEV_PUBKEY_FILE}")
    print(f"  公钥 hex: {pub_hex}")
    print(f"\n  loader.py 已配置从 {DEV_PUBKEY_FILE} 读取开发公钥。")
    print(f"  如需禁用开发模式，删除 {DEV_PUBKEY_FILE} 即可。")
    print("=" * 60)


if __name__ == "__main__":
    main()
