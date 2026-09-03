"""沙箱执行器 — 在隔离子进程中运行 SKILL.md 中的代码示例

策略:
  - 使用 subprocess 在隔离子进程运行
  - 自动 pip install 缺失依赖
  - 自动创建桩输入文件
  - 30 秒超时防止死循环
  - 临时目录运行，执行完清理
"""

import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .models import SandboxResult

logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────

TIMEOUT_SECONDS = 30
"""单段代码执行超时"""

MAX_OUTPUT_CHARS = 10000
"""标准输出截断长度"""

PIP_INSTALL_TIMEOUT = 30
"""pip install 超时"""

VENV_CREATE_TIMEOUT = 60
"""venv 创建超时"""

# 安全环境变量白名单（仅传递系统必需变量，排除 API Key 等敏感信息）
SAFE_ENV_KEYS = {
    "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP",
    "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "COMSPEC", "PATHEXT", "PROCESSOR_ARCHITECTURE",
    "NUMBER_OF_PROCESSORS", "OS", "PROCESSOR_IDENTIFIER",
    "PROCESSOR_LEVEL", "PROCESSOR_REVISION",
}


def _get_venv_python(venv_dir: str) -> str:
    """获取 venv 中的 Python 解释器路径"""
    if sys.platform == "win32":
        return os.path.join(venv_dir, "Scripts", "python.exe")
    return os.path.join(venv_dir, "bin", "python")


def _create_temp_venv(tmpdir: str) -> str | None:
    """在临时目录创建隔离 venv，返回 python 解释器路径"""
    venv_dir = os.path.join(tmpdir, ".sandbox_venv")
    try:
        subprocess.run(
            [sys.executable, "-m", "venv", venv_dir],
            capture_output=True, text=True,
            timeout=VENV_CREATE_TIMEOUT,
        )
        python_exe = _get_venv_python(venv_dir)
        if os.path.isfile(python_exe):
            return python_exe
        logger.warning("venv python not found at %s", python_exe)
        return None
    except Exception as e:
        logger.warning("Failed to create temp venv: %s", e)
        return None


# ── 代码块提取 ────────────────────────────────────────


def extract_code_blocks(skill_dir: str) -> list[dict]:
    """从 SKILL.md 提取 Python 代码块

    Returns:
        [{"language": "python", "code": "...", "line": 12}, ...]
    """
    skill_md = Path(skill_dir) / "SKILL.md"
    if not skill_md.exists():
        logger.warning("SKILL.md not found in %s", skill_dir)
        return []

    md_text = skill_md.read_text(encoding="utf-8")
    blocks = []
    lines = md_text.split("\n")
    in_code = False
    current_lang = ""
    current_code: list[str] = []
    start_line = 0

    for i, line in enumerate(lines):
        if line.startswith("```"):
            if in_code:
                code = "\n".join(current_code)
                if current_lang == "python":
                    blocks.append({
                        "language": current_lang,
                        "code": code,
                        "line": start_line + 1,
                    })
                current_code = []
                in_code = False
            else:
                current_lang = line[3:].strip()
                start_line = i
                in_code = True
        elif in_code:
            current_code.append(line)

    return blocks


# ── 依赖自动安装 ──────────────────────────────────────


def _extract_imports(code: str) -> list[str]:
    """从代码中提取所有顶层导入的包名"""
    imports = set()
    for line in code.split("\n"):
        line = line.strip()
        # import X, import X.Y.Z
        m = re.match(r"^import\s+(\S+)", line)
        if m:
            top = m.group(1).split(".")[0]
            # 跳过标准库
            if top not in ("os", "sys", "json", "re", "math", "datetime",
                           "time", "pathlib", "collections", "itertools",
                           "functools", "typing", "tempfile", "shutil",
                           "glob", "hashlib", "copy", "abc", "enum"):
                imports.add(top)
        # from X import Y
        m = re.match(r"^from\s+(\S+)", line)
        if m:
            top = m.group(1).split(".")[0]
            if top not in ("os", "typing"):
                imports.add(top)
    return sorted(imports)


def _try_install_deps(packages: list[str], python_exe: str) -> list[str]:
    """尝试在隔离 venv 环境 pip install 缺失依赖，返回失败的包名列表"""
    if not packages:
        return []

    failed = []
    for pkg in packages:
        # 先检查是否已安装（在隔离 venv 中）
        result = subprocess.run(
            [python_exe, "-c", f"import {pkg}"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            continue

        # 特殊处理 import 名 != pip 包名 的映射
        pip_name = {"easyocr": "easyocr",
                     "docx": "python-docx",
                     "pptx": "python-pptx",
                     "pypdf": "pypdf",
                     "pdfplumber": "pdfplumber",
                     "PIL": "pillow",
                     "graphviz": "graphviz"}.get(pkg, pkg)

        logger.info("pip installing %s (venv) ...", pip_name)
        try:
            r = subprocess.run(
                [python_exe, "-m", "pip", "install", pip_name],
                capture_output=True, text=True,
                timeout=PIP_INSTALL_TIMEOUT,
            )
            if r.returncode != 0:
                logger.warning("pip install %s failed: %s", pip_name, r.stderr[:200])
                failed.append(pkg)
            else:
                logger.info("pip install %s OK", pip_name)
        except subprocess.TimeoutExpired:
            logger.warning("pip install %s timed out", pip_name)
            failed.append(pkg)
        except Exception as e:
            logger.warning("pip install %s error: %s", pip_name, e)
            failed.append(pkg)

    return failed


# ── 输入文件桩 ────────────────────────────────────────


def _infer_input_files(code: str) -> list[str]:
    """从代码中推测可能需要的输入文件路径（相对路径）"""
    files = set()

    # 常见读取模式
    patterns = [
        # pd.read_csv("file.csv"), pd.read_excel("file.xlsx")
        r'(?:read_csv|read_excel|read_json|read_table)\s*\(\s*["\']([^"\']+)["\']',
        # Image.open("file.png")
        r'Image\.open\s*\(\s*["\']([^"\']+)["\']',
        # reader.readtext("file.jpg")
        r'readtext\s*\(\s*["\']([^"\']+)["\']',
        # open("file", "r"), open("file", encoding=...)
        r'open\s*\(\s*["\']([^"\']+)["\']\s*(?:,|\))',
        # SimpleDocTemplate("file.pdf")
        r'SimpleDocTemplate\s*\(\s*["\']([^"\']+)["\']',
        # ImageFont.truetype("file.ttf")
        r'truetype\s*\(\s*["\']([^"\']+)["\']',
    ]

    for pat in patterns:
        for m in re.finditer(pat, code):
            fname = m.group(1)
            # 排除 open() 写模式（如 open("out.csv", "w")）
            after = code[m.end():].lstrip()
            if after.startswith(("'w'", '"w"', "'wb'", '"wb"', "'a'", '"ab"')):
                continue
            # 排除明显是输出的文件
            if fname.endswith((".xlsx", ".pptx", ".docx")) and "read" not in pat:
                continue
            files.add(fname)

    return sorted(files)


def _create_stub_file(filepath: str, tmpdir: str) -> None:
    """创建桩文件用于沙箱测试"""
    full_path = os.path.join(tmpdir, filepath)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)

    ext = os.path.splitext(filepath)[1].lower()

    if ext == ".csv":
        with open(full_path, "w", encoding="utf-8") as f:
            f.write("name,value,count\nsample,100,1\n")
    elif ext == ".json":
        with open(full_path, "w", encoding="utf-8") as f:
            f.write('{"data": "sample"}')
    elif ext in (".txt", ".md"):
        with open(full_path, "w", encoding="utf-8") as f:
            f.write("sample content\n")
    elif ext in (".jpg", ".jpeg", ".png", ".gif", ".bmp"):
        _create_stub_image(full_path, ext)
    elif ext == ".ttf":
        # 无法创建真实字体文件，跳过
        pass
    elif ext in (".xlsx", ".docx", ".pptx", ".pdf"):
        # 创建空文件，库可能报错但代码逻辑能走到
        with open(full_path, "wb") as f:
            f.write(b"")
    else:
        # 默认创建空文件
        with open(full_path, "wb") as f:
            f.write(b"")

    logger.debug("Stub file created: %s", filepath)


def _create_stub_image(path: str, ext: str) -> None:
    """尝试创建桩图片文件"""
    try:
        from PIL import Image
        img = Image.new("RGB", (100, 100), color=(255, 255, 255))
        img.save(path, format="PNG" if ext == ".png" else "JPEG")
    except ImportError:
        # PIL 不可用时创建空文件
        with open(path, "wb") as f:
            f.write(b"")


# ── 代码执行 ──────────────────────────────────────────


def _build_sandbox_script(code: str, tmpdir: str) -> str:
    """构建沙箱脚本：preamble + 用户代码"""
    preamble = f"""# 沙箱安全限制
import os, sys, tempfile
os.environ["SANDBOX"] = "1"
os.environ["SKILL_HOME"] = {repr(tmpdir)}
os.chdir({repr(tmpdir)})
"""
    return preamble + "\n" + code


def _classify_error(stderr: str) -> str:
    """分类错误类型"""
    if "ModuleNotFoundError" in stderr or "ImportError" in stderr:
        return "DEPENDENCY"
    if "FileNotFoundError" in stderr:
        return "INPUT_FILE"
    if "TimeoutExpired" in stderr or "超时" in stderr:
        return "TIMEOUT"
    if "PermissionError" in stderr:
        return "PERMISSION"
    if "SyntaxError" in stderr:
        return "SYNTAX"
    return "RUNTIME"


def run_code_in_sandbox(code: str, timeout: int = TIMEOUT_SECONDS) -> SandboxResult:
    """在隔离 venv 子进程中运行 Python 代码"""
    with tempfile.TemporaryDirectory(prefix="sandbox_") as tmpdir:
        script_path = os.path.join(tmpdir, "_sandbox_script.py")

        # 创建桩输入文件
        input_files = _infer_input_files(code)
        for f in input_files:
            _create_stub_file(f, tmpdir)

        # 创建隔离 venv（若失败则回退到宿主 python）
        venv_python = _create_temp_venv(tmpdir)
        python_exe = venv_python if venv_python else sys.executable

        # 在隔离 venv 中安装依赖
        imports = _extract_imports(code)
        failed_deps = _try_install_deps(imports, python_exe)

        # 构建安全环境变量（白名单，不含 API Key）
        safe_env = {k: os.environ[k] for k in SAFE_ENV_KEYS if k in os.environ}
        safe_env.update({
            "SKILL_HOME": tmpdir,
            "SANDBOX": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        if venv_python:
            # 确保 venv 的 Scripts/bin 在 PATH 首位
            venv_bin = os.path.dirname(python_exe)
            old_path = safe_env.get("PATH", "")
            safe_env["PATH"] = f"{venv_bin}{os.pathsep}{old_path}"

        # 构建完整脚本
        full_script = _build_sandbox_script(code, tmpdir)

        with open(script_path, "w", encoding="utf-8") as f:
            f.write(full_script)

        start = time.monotonic()
        try:
            proc = subprocess.run(
                [python_exe, script_path],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=tmpdir,
                env=safe_env,
            )
            elapsed_ms = int((time.monotonic() - start) * 1000)
            error_type = _classify_error(proc.stderr) if proc.returncode != 0 else ""

            return SandboxResult(
                passed=proc.returncode == 0,
                stdout=proc.stdout[:MAX_OUTPUT_CHARS],
                stderr=proc.stderr[:MAX_OUTPUT_CHARS],
                exit_code=proc.returncode,
                duration_ms=elapsed_ms,
                error_type=error_type,
            )
        except subprocess.TimeoutExpired:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            return SandboxResult(
                passed=False,
                stderr=f"执行超时 ({timeout}s)",
                timeout=True,
                duration_ms=elapsed_ms,
                error_type="TIMEOUT",
            )
        except Exception as e:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            return SandboxResult(
                passed=False,
                stderr=str(e)[:MAX_OUTPUT_CHARS],
                duration_ms=elapsed_ms,
                error_type="RUNTIME",
            )


def run_sandbox(skill_dir: str) -> SandboxResult:
    """对 SKILL.md 中的所有 Python 代码块运行沙箱测试

    逐个执行代码块，返回第一个成功的结果或最佳失败结果。
    """
    blocks = extract_code_blocks(skill_dir)

    if not blocks:
        return SandboxResult(passed=True, stdout="无代码块需执行")

    # 筛选出有实质逻辑的代码块
    candidates = []
    for block in blocks:
        code = block["code"]
        lines = [l for l in code.split("\n") if l.strip() and not l.strip().startswith("#")]
        non_import_lines = [l for l in lines if not l.strip().startswith(("import ", "from "))]
        if len(non_import_lines) >= 2:
            candidates.append(block)

    if not candidates:
        return SandboxResult(passed=True, stdout="仅 import 片段，跳过沙箱执行")

    # 逐个尝试代码块，直到有一个通过
    last_result = None
    for block in candidates:
        logger.info("Sandbox running code block at line %d (%d chars)",
                     block["line"], len(block["code"]))
        result = run_code_in_sandbox(block["code"])
        if result.passed:
            return result
        last_result = result
        logger.info("Block at line %d failed: %s", block["line"], result.stderr[:100])

    # 全都失败了，返回最后一个结果（带总览信息）
    assert last_result is not None
    total = len(candidates)
    last_result.stderr = (
        f"[已尝试 {total} 个代码块，均未通过]\n"
        f"最后一个错误: {last_result.stderr}"
    )[:MAX_OUTPUT_CHARS]
    return last_result
