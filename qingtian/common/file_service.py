"""通用文件服务 — 上传/下载/分块拼接，供所有模块共用。

模块集成方式:
  from common.file_service import router as file_router
  app.include_router(file_router)  # 注册到 main.py

API 端点:
  POST /v1/files/upload       单文件上传 (≤10MB)
  POST /v1/files/chunk         分块上传 (>10MB，客户端自动分块)
  GET  /v1/files/{id}/download 文件下载
"""

import hashlib as _hashlib
import logging
import os as _os
import tempfile as _tempfile
from datetime import datetime

from fastapi import APIRouter, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

logger = logging.getLogger("file_service")

router = APIRouter(prefix="/v1/files", tags=["文件服务"])

# 存储目录（可环境变量覆盖）
_FILE_DIR = _os.environ.get("QINGTIAN_FILE_DIR", _os.path.join(_tempfile.gettempdir(), "qingtian_files"))
_CHUNK_DIR = _os.path.join(_FILE_DIR, "chunks")

_os.makedirs(_FILE_DIR, exist_ok=True)
_os.makedirs(_CHUNK_DIR, exist_ok=True)


def _validate_path_safe(component: str, name: str = "path") -> str:
    """防止路径遍历：拒绝含 ../ 或绝对路径的组件"""
    if not component or component.startswith("/"):
        raise HTTPException(400, detail=f"{name} 包含非法路径")
    # 标准化和解码后检查路径穿越
    norm = _os.path.normpath(component)
    if norm.startswith("..") or ".." in norm.split(_os.sep):
        raise HTTPException(400, detail=f"{name} 包含非法路径跳转")
    if _os.sep in component or (chr(92) in component):  # / 或 \
        raise HTTPException(400, detail=f"{name} 包含路径分隔符")
    return component


def _validate_filename(filename: str, name: str = "filename") -> str:
    """防止路径穿越：只允许文件名，拒绝 /、\\、.. 或绝对路径"""
    if not filename:
        raise HTTPException(400, detail=f"{name} 为空")
    norm = filename.replace("\\", "/")
    if norm.startswith("/") or "/" in norm or ".." in norm:
        raise HTTPException(400, detail=f"{name} 包含非法路径")
    return filename


# ═══════════════════════════════════════════════════════════════
# UI 页面
# ═══════════════════════════════════════════════════════════════

@router.get("/", response_class=HTMLResponse)
async def file_page():
    """通用文件服务 Web 页面（上传 + 下载）。"""
    return HTMLResponse(_FILE_PAGE_HTML)


# ═══════════════════════════════════════════════════════════════
# 上传
# ═══════════════════════════════════════════════════════════════

@router.post("/upload")
async def upload(
    file: UploadFile = File(...),
    enterprise_id: str = Header(default="default", alias="X-Enterprise-ID"),
    module: str = Form(default=""),
    tags: str = Form(default=""),
):
    """单文件上传（≤10MB）。大文件请用 /chunk 分块上传。

    企业隔离: 文件按 enterprise_id 分目录存储。
    认证: X-Enterprise-ID header 必填，后端通过 nginx/镇岳 token 做前置校验。
    """
    if not enterprise_id or enterprise_id == "default":
        raise HTTPException(401, detail="X-Enterprise-ID header 必填")
    _validate_path_safe(enterprise_id, "X-Enterprise-ID")
    content = await file.read()
    file_id = _hashlib.sha256(content).hexdigest()[:16]
    filename = _validate_filename(file.filename or "upload")

    # 企业隔离目录
    ent_dir = _os.path.join(_FILE_DIR, enterprise_id)
    _os.makedirs(ent_dir, exist_ok=True)
    file_path = _os.path.join(ent_dir, f"{file_id}_{filename}")

    with open(file_path, "wb") as f:
        f.write(content)

    logger.info("上传完成: id=%s name=%s size=%d ent=%s module=%s",
                file_id, filename, len(content), enterprise_id, module or "-")

    return {
        "ok": True,
        "file_id": file_id,
        "filename": filename,
        "size": len(content),
        "enterprise_id": enterprise_id,
    }


@router.post("/chunk")
async def upload_chunk(
    chunk: UploadFile = File(...),
    chunk_index: int = Form(...),
    total_chunks: int = Form(...),
    filename: str = Form(...),
    file_size: int = Form(...),
    file_id: str = Form(default=""),
    enterprise_id: str = Header(default="default", alias="X-Enterprise-ID"),
    module: str = Form(default=""),
):
    """分块上传（>10MB 文件）。客户端自动切 10MB 块，断点可续。

    首次请求不传 file_id → 服务器生成 → 客户端缓存；
    后续块传 file_id → 拼接校验。
    """
    if not file_id:
        file_id = _hashlib.sha256(
            f"{enterprise_id}_{filename}_{file_size}_{_os.urandom(8)}".encode()
        ).hexdigest()[:16]
    # C13 (R11): file_id 由客户端传入，未校验可含 ../ 写任意路径——同企业校验
    _validate_path_safe(file_id, "file_id")

    # 企业隔离分块目录 + 文件名穿越防护
    _validate_path_safe(enterprise_id, "X-Enterprise-ID")
    filename = _validate_filename(filename)
    ent_chunk_dir = _os.path.join(_CHUNK_DIR, enterprise_id)
    _os.makedirs(ent_chunk_dir, exist_ok=True)

    chunk_data = await chunk.read()
    part_path = _os.path.join(ent_chunk_dir, f"{file_id}.part{chunk_index:04d}")
    with open(part_path, "wb") as f:
        f.write(chunk_data)

    logger.debug("分块: file_id=%s %d/%d size=%d", file_id, chunk_index + 1, total_chunks, len(chunk_data))

    # 检查收齐
    all_parts = sorted([p for p in _os.listdir(ent_chunk_dir) if p.startswith(file_id)])
    if len(all_parts) < total_chunks:
        return {"ok": True, "file_id": file_id, "chunk": chunk_index, "status": "uploading"}

    # 拼接
    ent_dir = _os.path.join(_FILE_DIR, enterprise_id)
    _os.makedirs(ent_dir, exist_ok=True)
    final_path = _os.path.join(ent_dir, f"{file_id}_{filename}")

    with open(final_path, "wb") as out:
        for part_name in all_parts:
            with open(_os.path.join(ent_chunk_dir, part_name), "rb") as inp:
                out.write(inp.read())
            _os.remove(_os.path.join(ent_chunk_dir, part_name))

    actual_size = _os.path.getsize(final_path)
    logger.info("分块拼接完成: id=%s name=%s size=%d expected=%d ent=%s module=%s",
                file_id, filename, actual_size, file_size, enterprise_id, module or "-")

    return {"ok": True, "file_id": file_id, "filename": filename, "size": actual_size, "status": "complete"}


# ═══════════════════════════════════════════════════════════════
# 下载
# ═══════════════════════════════════════════════════════════════

@router.get("/{file_id}/download")
async def download(
    file_id: str,
    enterprise_id: str = Header(default="default", alias="X-Enterprise-ID"),
):
    """下载文件。企业隔离: 只能下载本企业目录下的文件。

    C13 (R11): 移除 ?ent= 查询参数覆写——此前可跨企业读任意文件；
    企业标识一律取自 X-Enterprise-ID header。
    """
    _validate_path_safe(file_id, "file_id")
    _validate_path_safe(enterprise_id, "X-Enterprise-ID")
    ent_dir = _os.path.join(_FILE_DIR, enterprise_id)
    for fname in _os.listdir(ent_dir) if _os.path.exists(ent_dir) else []:
        if fname.startswith(file_id + "_"):
            original_name = fname[len(file_id) + 1:]
            file_path = _os.path.join(ent_dir, fname)
            logger.info("下载: id=%s name=%s size=%d ent=%s", file_id, original_name, _os.path.getsize(file_path), enterprise_id)
            return FileResponse(file_path, filename=original_name, media_type="application/octet-stream")
    raise HTTPException(404, detail=f"文件不存在: {file_id}")


# ═══════════════════════════════════════════════════════════════
# 管理
# ═══════════════════════════════════════════════════════════════

@router.get("/list")
async def list_files(
    enterprise_id: str = Header(default="default", alias="X-Enterprise-ID"),
):
    """列出本企业已上传的文件"""
    _validate_path_safe(enterprise_id, "X-Enterprise-ID")
    ent_dir = _os.path.join(_FILE_DIR, enterprise_id)
    if not _os.path.exists(ent_dir):
        return {"files": []}
    files = []
    for fname in _os.listdir(ent_dir):
        path = _os.path.join(ent_dir, fname)
        parts = fname.split("_", 1)
        files.append({
            "file_id": parts[0],
            "filename": parts[1] if len(parts) > 1 else fname,
            "size": _os.path.getsize(path),
            "uploaded_at": datetime.fromtimestamp(_os.path.getmtime(path)).isoformat(),
        })
    return {"files": sorted(files, key=lambda x: x["uploaded_at"], reverse=True)}


@router.delete("/{file_id}")
async def delete_file(
    file_id: str,
    enterprise_id: str = Header(default="default", alias="X-Enterprise-ID"),
):
    """删除文件（企业标识仅取 X-Enterprise-ID header，C13/R11 移除 ?ent= 覆写）"""
    _validate_path_safe(file_id, "file_id")
    _validate_path_safe(enterprise_id, "X-Enterprise-ID")
    ent_dir = _os.path.join(_FILE_DIR, enterprise_id)
    for fname in _os.listdir(ent_dir) if _os.path.exists(ent_dir) else []:
        if fname.startswith(file_id + "_"):
            _os.remove(_os.path.join(ent_dir, fname))
            logger.info("删除: id=%s name=%s ent=%s", file_id, fname, enterprise_id)
            return {"ok": True, "file_id": file_id}
    raise HTTPException(404, detail=f"文件不存在: {file_id}")


# ═══════════════════════════════════════════════════════════════
# Web 页面（单页，所有模块共用）
# ═══════════════════════════════════════════════════════════════

_FILE_PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>文件服务 — ACSSA</title>
<style>
:root{--bg:#f5f7fa;--card:#fff;--text:#1a1a2e;--sub:#6b7280;--primary:#4f46e5;--primary-h:#4338ca;--green:#10b981;--red:#ef4444;--border:#e5e7eb;--radius:8px}
*{margin:0;padding:0;box-sizing:border-box}
body{font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:var(--bg);color:var(--text);min-height:100vh}
.topbar{background:var(--card);border-bottom:1px solid var(--border);padding:12px 24px;display:flex;align-items:center;justify-content:space-between}
.topbar h1{font-size:18px;color:var(--primary)}
.topbar select{padding:6px 12px;border:1px solid var(--border);border-radius:6px;font-size:13px}
.container{max-width:760px;margin:0 auto;padding:24px}
.card{background:var(--card);border-radius:var(--radius);box-shadow:0 1px 3px rgba(0,0,0,.08);padding:24px;margin-bottom:16px}
.card h2{font-size:16px;margin-bottom:16px}
.dropzone{border:2px dashed var(--border);border-radius:12px;padding:40px;text-align:center;cursor:pointer;transition:all .2s}
.dropzone:hover,.dragover{border-color:var(--primary);background:rgba(79,70,229,.04)}
.dropzone .icon{font-size:36px;margin-bottom:8px}
.dropzone p{color:var(--sub);font-size:13px}
.progress-bar{height:6px;background:var(--border);border-radius:3px;overflow:hidden;margin-top:12px}
.progress-bar .fill{height:100%;background:var(--primary);transition:width .3s;width:0;border-radius:3px}
.form-row{display:flex;gap:12px;margin-bottom:12px;align-items:flex-end;flex-wrap:wrap}
.form-row label{font-size:12px;color:var(--sub);display:block;margin-bottom:4px}
.form-row input{min-width:240px;padding:8px 12px;border:1px solid var(--border);border-radius:6px;font-size:14px;outline:none}
.form-row input:focus{border-color:var(--primary)}
.btn{padding:8px 20px;border:none;border-radius:6px;font-size:14px;cursor:pointer;font-weight:500}
.btn-primary{background:var(--primary);color:#fff}.btn-primary:hover{background:var(--primary-h)}
.btn-outline{background:var(--card);border:1px solid var(--border)}.btn-outline:hover{border-color:var(--primary)}
.file-list{list-style:none;margin-top:12px}
.file-list li{display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--border);font-size:13px}
.file-list .name{flex:1}.file-list .actions{display:flex;gap:8px}
.file-list .actions button{font-size:11px;padding:2px 8px;border:1px solid var(--border);border-radius:4px;cursor:pointer;background:var(--card)}
.toast{position:fixed;top:16px;right:16px;padding:12px 20px;border-radius:var(--radius);color:#fff;font-size:13px;z-index:999;animation:in .3s}
.toast.error{background:var(--red)}.toast.success{background:var(--green)}
@keyframes in{from{transform:translateX(100%);opacity:0}to{transform:translateX(0);opacity:1}}
.hidden{display:none!important}
</style>
</head>
<body>
<header class="topbar"><h1>📁 文件服务</h1>
  <div style="display:flex;gap:12px;align-items:center">
    <select id="entSelect"><option value="">选择部门...</option></select>
    <span style="font-size:12px;color:var(--sub)" id="fileCount"></span>
  </div>
</header>
<main class="container">
  <div class="card">
    <h2>📤 上传文件</h2>
    <div class="dropzone" id="dropzone">
      <div class="icon">📁</div><p>拖拽文件到此处，或点击选择</p>
      <p style="font-size:11px;color:#999;margin-top:4px">支持 PDF/DOCX/XLSX/图片，最大 500MB</p>
      <input type="file" id="fileInput" multiple style="display:none" accept="*">
    </div>
    <div class="progress-bar hidden" id="progWrap"><div class="fill" id="progBar"></div></div>
    <p style="font-size:12px;color:var(--sub);margin-top:4px" id="progText"></p>
  </div>
  <div class="card">
    <h2>📥 下载文件</h2>
    <div class="form-row">
      <div><label>文件 ID</label><input type="text" id="dlId" placeholder="输入文件 ID"></div>
      <div style="align-self:flex-end"><button class="btn btn-primary" onclick="doDownload()">下载</button></div>
    </div>
    <div class="form-row" style="margin-top:8px">
      <div><label>输入 secret key</label><input type="text" id="secretKey" placeholder="可选: 输入 secret key"></div>
      <div style="align-self:flex-end">
        <button class="btn btn-primary" onclick="doDownload()">下载</button>
      </div>
    </div>
  </div>
  <div class="card">
    <h2>📋 已上传文件</h2>
    <ul class="file-list" id="fileList"><li style="color:var(--sub)">选择部门后加载...</li></ul>
  </div>
</main>
<script>
let ENT = localStorage.getItem('file_ent') || 'default';
document.getElementById('entSelect').value = ENT;
document.getElementById('entSelect').addEventListener('change', e => { ENT=e.target.value;localStorage.setItem('file_ent',ENT);loadFiles(); });
document.getElementById('dropzone').addEventListener('click',()=>document.getElementById('fileInput').click());
['dragover','dragleave','drop'].forEach(ev=>document.getElementById('dropzone').addEventListener(ev,e=>{e.preventDefault();if(ev!=='drop')e.target.classList[ev==='dragover'?'add':'remove']('dragover');}));
document.getElementById('dropzone').addEventListener('drop',e=>{e.target.classList.remove('dragover');handleFiles(e.dataTransfer.files);});
document.getElementById('fileInput').addEventListener('change',e=>handleFiles(e.target.files));

async function handleFiles(files){const l=document.getElementById('fileList');for(const f of files){const li=document.createElement('li');li.innerHTML=`<span class="name">${f.name} (${fmtSize(f.size)})</span><span style="font-size:12px;color:var(--sub)">上传中...</span>`;l.prepend(li);try{await up(f,li)}catch(e){li.querySelector('span:last-child').textContent='❌ '+e.message;li.querySelector('span:last-child').style.color='var(--red)'}}}
async function up(file,li){const C=10*1024*1024;const total=file.size;const bar=document.getElementById('progBar');const wrap=document.getElementById('progWrap');const txt=document.getElementById('progText');if(total<=C){const fd=new FormData();fd.append('file',file);const r=await fetch('/v1/files/upload',{method:'POST',headers:{'X-Enterprise-ID':ENT},body:fd});if(!r.ok)throw new Error(await r.text());bar.style.width='100%';txt.textContent=file.name+' 完成';setTimeout(()=>{bar.style.width='0';wrap.classList.add('hidden');txt.textContent=''},2000);li.querySelector('span:last-child').textContent='✅ 完成';li.querySelector('span:last-child').style.color='var(--green)';loadFiles();return}wrap.classList.remove('hidden');const chunks=Math.ceil(total/C);let fid='';for(let i=0;i<chunks;i++){const s=i*C,e=Math.min(s+C,total);const fd=new FormData();fd.append('chunk',file.slice(s,e),file.name+'.part'+i);fd.append('chunk_index',i);fd.append('total_chunks',chunks);fd.append('filename',file.name);fd.append('file_size',total);if(fid)fd.append('file_id',fid);let ok=0;for(let t=0;t<3&&!ok;t++){try{const r=await fetch('/v1/files/chunk',{method:'POST',headers:{'X-Enterprise-ID':ENT},body:fd});if(!r.ok)throw new Error(await r.text());const d=await r.json();fid=d.file_id;const pct=Math.round(e/total*100);bar.style.width=pct+'%';txt.textContent=`${file.name}: ${fmtSize(e)}/${fmtSize(total)}`;ok=1}catch(ex){if(t===2)throw ex;await new Promise(r=>setTimeout(r,1000))}}}li.querySelector('span:last-child').textContent='✅ '+fid;li.querySelector('span:last-child').style.color='var(--green)';setTimeout(()=>{bar.style.width='0';wrap.classList.add('hidden');txt.textContent=''},2000);loadFiles()}
async function doDownload(){const id=document.getElementById('dlId').value.trim();if(!id)return toast('请输入文件 ID','error');window.open('/v1/files/'+id+'/download?ent='+ENT,'_blank')}
async function loadFiles(){try{const r=await fetch('/v1/files/list',{headers:{'X-Enterprise-ID':ENT}});if(!r.ok)return;const d=await r.json();const l=document.getElementById('fileList');if(!d.files.length){l.innerHTML='<li style="color:var(--sub)">暂无文件</li>';document.getElementById('fileCount').textContent='';return}document.getElementById('fileCount').textContent=d.files.length+' 个文件';l.innerHTML=d.files.map(f=>`<li><span class="name">${f.filename} <span style="color:var(--sub);font-size:11px">${f.file_id}</span></span><span class="actions"><span style="font-size:11px;color:var(--sub);margin-right:8px">${fmtSize(f.size)}</span><button onclick="window.open('/v1/files/${f.file_id}/download?ent=${ENT}','_blank')">下载</button><button onclick="del('${f.file_id}')" style="color:var(--red)">删除</button></span></li>`).join('')}catch(e){console.error(e)}}
async function del(id){if(!confirm('确认删除 '+id+'?'))return;await fetch('/v1/files/'+id,{method:'DELETE',headers:{'X-Enterprise-ID':ENT}});loadFiles();toast('已删除','success')}
function toast(m,t){const e=document.createElement('div');e.className='toast '+t;e.textContent=m;document.body.appendChild(e);setTimeout(()=>e.remove(),3000)}
function fmtSize(b){return b<1024*1024?(b/1024).toFixed(1)+'KB':(b/1024/1024).toFixed(1)+'MB'}
loadFiles();
</script>
</body>
</html>"""
