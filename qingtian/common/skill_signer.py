"""Skill 签名工具 — 开发环境临时签名

依赖 common.crypto Ed25519 实现。

用法:
    python -m common.skill_signer sign <skill.json>        # 用 dev 私钥签名
    python -m common.skill_signer verify <skill.json>      # 验证签名
    python -m common.skill_signer genkey                   # 生成新密钥对
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from common.crypto import (
    DEV_PUBLIC_KEY_HEX,
    generate_keypair,
    sign,
    verify,
)
from common.skill_manifest import canonical_json


def _load_raw(path: Path) -> tuple[dict, str]:
    """读取 skill.json，返回 (数据, 原 certificate hex)"""
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    sig_hex = raw.get("certificate", "") or ""
    raw.pop("certificate", None)
    return raw, sig_hex


def _write_signed(path: Path, raw: dict, sig_hex: str) -> None:
    """写回 skill.json（保留原格式）"""
    raw["certificate"] = sig_hex
    with open(path, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"  [OK] 已写入 {path}")


def cmd_sign(args: argparse.Namespace) -> None:
    """用 dev 私钥签名 skill.json"""
    path = Path(args.path)
    if not path.exists():
        print(f"  [ERR] 文件不存在: {path}", file=sys.stderr)
        sys.exit(1)

    raw, _ = _load_raw(path)
    if not args.key:
        print("  [ERR] 必须显式提供 --key 私钥（私钥不入库，须从安全渠道获取）", file=sys.stderr)
        sys.exit(1)
    payload = canonical_json(raw).encode("utf-8")
    private_key = bytes.fromhex(args.key)
    public_key = bytes.fromhex(args.pub or DEV_PUBLIC_KEY_HEX)

    sig = sign(private_key, payload)
    sig_hex = sig.hex()

    _write_signed(path, raw, sig_hex)

    # 验证写回是否正确
    check_raw, check_sig = _load_raw(path)
    check_payload = canonical_json(check_raw).encode("utf-8")
    if verify(public_key, check_payload, bytes.fromhex(check_sig)):
        print(f"  [OK] 签名验证通过 (public key: {public_key.hex()[:16]}...)")
    else:
        print(f"  [ERR] 签名验证失败！", file=sys.stderr)
        sys.exit(1)


def cmd_verify(args: argparse.Namespace) -> None:
    """验证 skill.json 签名"""
    path = Path(args.path)
    if not path.exists():
        print(f"  [ERR] 文件不存在: {path}", file=sys.stderr)
        sys.exit(1)

    raw, sig_hex = _load_raw(path)
    if not sig_hex:
        print(f"  [ERR] certificate 为空，未签名")
        sys.exit(1)

    payload = canonical_json(raw).encode("utf-8")
    sig = bytes.fromhex(sig_hex)

    # 先试 dev 公钥，再试自定义公钥
    public_keys = [bytes.fromhex(DEV_PUBLIC_KEY_HEX)]
    if args.pub:
        public_keys.insert(0, bytes.fromhex(args.pub))

    for pub in public_keys:
        if verify(pub, payload, sig):
            print(f"  [OK] 签名有效 (public key: {pub.hex()[:16]}...)")
            return

    print(f"  [ERR] 签名无效", file=sys.stderr)
    sys.exit(1)


def cmd_genkey(_args: argparse.Namespace) -> None:
    """生成新的 Ed25519 密钥对"""
    priv, pub = generate_keypair()
    print(f"私钥: {priv.hex()}")
    print(f"公钥: {pub.hex()}")
    print()
    print("将公钥写入 common/crypto.py DEV_PUBLIC_KEY_HEX")
    print("将私钥用于签名（--key 参数）")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Skill 签名工具 — 开发环境临时签名",
    )
    parser.add_argument("--key", default="", help="Ed25519 私钥 hex（签名必填，私钥不入库）")
    parser.add_argument("--pub", default="", help="Ed25519 公钥 hex（默认用 dev 公钥）")

    sub = parser.add_subparsers(dest="cmd", required=True)

    p_sign = sub.add_parser("sign", help="签名 skill.json")
    p_sign.add_argument("path", help="skill.json 路径")

    p_verify = sub.add_parser("verify", help="验证 skill.json 签名")
    p_verify.add_argument("path", help="skill.json 路径")

    sub.add_parser("genkey", help="生成新密钥对")

    args = parser.parse_args()
    handlers = {"sign": cmd_sign, "verify": cmd_verify, "genkey": cmd_genkey}
    handlers[args.cmd](args)


if __name__ == "__main__":
    main()
