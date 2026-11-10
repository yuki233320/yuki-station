#!/usr/bin/env python3
"""Local workspace with optional, session-only live model connections."""
import argparse
import base64
import copy
import hashlib
import io
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import shutil
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote
import webbrowser
import zipfile

from service_lock import ServiceDataLock

HERE = Path(__file__).resolve().parent
MAX_FILE = 20 * 1024 * 1024
MAX_WORKSPACE = 80 * 1024 * 1024
MODELS = ["豆包", "Grok", "Claude", "Codex", "自定义执行器"]
STEPS = [("采集资料", "豆包", "collect"), ("分析整理", "Grok", "analyze"), ("制定方案", "Claude", "plan"), ("制作交付", "Codex", "deliver")]

def stamp():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")

def uid():
    return secrets.token_hex(6)

def dumps(obj):
    return json.dumps(obj, ensure_ascii=False, indent=2)

def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def safe_path(root, name):
    if not isinstance(name, str) or not name or "\x00" in name or "\\" in name:
        raise ValueError("无效文件路径")
    rel = Path(name)
    if rel.is_absolute() or any(p in ("..", ".") for p in rel.parts):
        raise ValueError("文件必须位于当前工作区")
    root = root.resolve()
    p = root.joinpath(rel)
    for parent in (p, *p.parents):
        if parent == root:
            break
        if parent.is_symlink():
            raise ValueError("不允许符号链接")
    if root not in p.resolve().parents:
        raise ValueError("文件必须位于当前工作区")
    return p

def inventory(root):
    result = []
    for p in sorted(root.rglob("*")):
        if p.is_symlink():
            raise ValueError("工作区内不允许符号链接")
        if p.is_file():
            result.append({"path": p.relative_to(root).as_posix(), "size": p.stat().st_size, "sha256": digest(p)})
    return result

class Store:
    """Each revision contains both state and files. Atomic HEAD replacement commits it."""
    def __init__(self, root, seed=True):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        if seed and not self.list_projects():
            self.create("视频调研与复刻", "把视频线索转为分析、方案与可交付文件。", "#5279bf", seed=True)
            self.create("外网资料整理", "归档公开来源，形成可追溯的资料摘要。", "#9a72b5")
            self.create("内容选题与交付", "整理选题、制作方案并交付可编辑文稿。", "#3e9a83")

    def project_dir(self, pid):
        if not re.fullmatch(r"[a-f0-9]{12}", pid or ""):
            raise ValueError("无效项目编号")
        p = self.root / pid
        if not p.is_dir():
            raise ValueError("项目不存在")
        return p

    def revision_dir(self, pid, rid):
        if not re.fullmatch(r"[a-f0-9]{12}", rid or ""):
            raise ValueError("无效版本编号")
        p = self.project_dir(pid) / "revisions" / rid
        if not p.is_dir():
            raise ValueError("版本不存在")
        return p

    def current(self, pid):
        p = self.project_dir(pid)
        rid = (p / "HEAD").read_text().strip()
        rev = self.revision_dir(pid, rid)
        return json.loads((rev / "state.json").read_text()), rev / "workspace"

    def list_projects(self):
        result = []
        for p in sorted(self.root.iterdir()):
            if p.is_dir() and (p / "HEAD").exists() and re.fullmatch(r"[a-f0-9]{12}", p.name):
                s, _ = self.current(p.name)
                result.append({k: s[k] for k in ("id", "title", "goal", "color", "active", "stages", "updated", "revision")})
        return result

    def publish(self, directory, stage, rid):
        os.replace(stage, directory / "revisions" / rid)
        head = directory / (".HEAD-" + rid)
        with head.open("w") as f:
            f.write(rid)
            f.flush()
            os.fsync(f.fileno())
        os.replace(head, directory / "HEAD")

    def create(self, title, goal, color="#5279bf", seed=False, steps=None, mode="demo", *, project_id=None, trigger_origin=None):
        if not isinstance(title, str) or not title.strip() or len(title) > 80:
            raise ValueError("项目名需为 1–80 个字符")
        if not isinstance(goal, str) or len(goal) > 5000:
            raise ValueError("目标过长")
        with self.lock:
            if project_id is not None and (not isinstance(project_id, str) or not re.fullmatch('[a-f0-9]{12}', project_id) or not isinstance(trigger_origin, str) or not trigger_origin.startswith('trigger:')):
                raise ValueError('固定项目编号只用于有来源证明的Trigger工作副本')
            pid, rid = project_id or uid(), uid()
            d = self.root / pid
            if d.exists():
                current, _ = self.current(pid)
                if project_id is not None and current.get('triggerOrigin') == trigger_origin:
                    return pid
                raise Conflict('项目编号已存在，不能覆盖其他工作区')
            (d / "revisions").mkdir(parents=True)
            temp = Path(tempfile.mkdtemp(prefix=".pending-", dir=d))
            w = temp / "workspace"
            (w / "assets").mkdir(parents=True)
            stages = [{"name": name, "model": model, "key": key, "folder": f"stages/{i+1:02d}-{key}", "status": "active" if i == 0 else "waiting", "session": uid() if i == 0 else None} for i, (name, model, key) in enumerate(steps or STEPS)]
            for st in stages:
                (w / st["folder"]).mkdir(parents=True)
            (w / "brief.md").write_text(f"# {title}\n\n{goal}\n\n模式：{mode}。实际调用与文件产物以执行记录为准。\n", encoding="utf8")
            if seed:
                (w / "assets" / "source-notes.txt").write_text("研究线索：https://www.douyin.com/video/7700658630564012153\n\n视频中的作品名：Lancer v0.0.1。\n核心需求：以文件与工作区完成任务交接，保留各阶段独立会话，支持版本回滚。\n\n此文件是演示输入，不包含下载的视频或视频转写。\n", encoding="utf8")
            state = {"id": pid, "title": title.strip(), "goal": goal, "color": color, "active": 0, "stages": stages, "revision": rid, "updated": stamp(), "events": [{"id": uid(), "time": stamp(), "type": "created", "title": "创建本地工作区", "detail": "项目已保存。可导入文件，逐步执行、交接或回滚。所有模型步骤均为本地演示。", "stage": 0, "revision": rid}]}
            state["mode"] = mode
            if trigger_origin is not None:
                state['triggerOrigin'] = trigger_origin
            if mode == "live":
                state["events"][0]["detail"] = "已创建真实模型演示工作区；尚未发起生成。"
            (temp / "state.json").write_text(dumps(state), encoding="utf8")
            (temp / "meta.json").write_text(dumps({"id": rid, "time": stamp(), "label": "初始工作区", "parent": None, "kind": "created"}), encoding="utf8")
            self.publish(d, temp, rid)
            return pid

    def commit(self, pid, label, kind, mutate, expected=None, source=None):
        with self.lock:
            current, current_w = self.current(pid)
            if expected and current["revision"] != expected:
                raise Conflict("工作区已有更新，请刷新后再操作；当前输入没有覆盖新版本。")
            d = self.project_dir(pid)
            rid = uid()
            origin = self.revision_dir(pid, source) if source else current_w.parent
            state = json.loads((origin / "state.json").read_text())
            state["events"] = copy.deepcopy(current["events"])
            state["revision"], state["updated"] = rid, stamp()
            temp = Path(tempfile.mkdtemp(prefix=".pending-", dir=d))
            try:
                # Reject externally inserted links before copying an old revision.
                inventory(origin / "workspace")
                shutil.copytree(origin / "workspace", temp / "workspace")
                details = mutate(state, temp / "workspace", current) or {}
                files = inventory(temp / "workspace")
                if sum(x["size"] for x in files) > MAX_WORKSPACE:
                    raise ValueError("此验证版每个工作区最多 80 MB。请使用较小素材。")
                e = {"id": uid(), "time": stamp(), "type": kind, "title": label, "stage": state["active"], "revision": rid, **details}
                state["events"].append(e)
                (temp / "state.json").write_text(dumps(state), encoding="utf8")
                meta = {"id": rid, "time": stamp(), "label": label, "kind": kind, "parent": current["revision"], "restoredFrom": source, "files": len(files)}
                (temp / "meta.json").write_text(dumps(meta), encoding="utf8")
                self.publish(d, temp, rid)
            finally:
                if temp.exists():
                    shutil.rmtree(temp)
            return self.view(pid)

    def view(self, pid):
        with self.lock:
            state, w = self.current(pid)
            result = copy.deepcopy(state)
            result["files"] = inventory(w)
            result["workspacePath"] = str(w)
            result["cwd"] = str(w / state["stages"][state["active"]]["folder"])
            revs = [json.loads(p.read_text()) for p in (self.project_dir(pid) / "revisions").glob("*/meta.json")]
            # Event order is authoritative when several writes have identical timestamps.
            order = {e["revision"]: i for i, e in enumerate(state["events"])}
            result["revisions"] = sorted(revs, key=lambda x: order.get(x["id"], -1), reverse=True)
            return result

    def complete(self, pid, expected):
        def act(s, w, previous):
            st = s["stages"][s["active"]]
            if st["status"] != "active":
                raise ValueError("当前步骤已完成，请先交接给下一步骤")
            inputs = [{**x, "path": "assets/" + x["path"]} for x in inventory(w / "assets")]
            if s["active"] > 0:
                handoff = json.loads((w / st["folder"] / "input" / "handoff.json").read_text())
                for artifact in handoff["files"]:
                    p = safe_path(w, artifact["target"])
                    if not p.is_file() or digest(p) != artifact["sha256"]:
                        raise ValueError("交接输入已变化，校验失败。请恢复交接版本，或将修改后的内容作为新资料导入。")
                inputs += inventory(w / st["folder"] / "input")
            output = f"{st['folder']}/output.md"
            rows = "\n".join(f"- {x['path']} · {x['size']} bytes · SHA-256 {x['sha256']}" for x in inputs) or "- 尚未导入资料"
            content = f"# {st['name']} / 本地步骤验证\n\n> 本文由确定性的本地程序生成，未调用 {st['model']}，不代表 AI 分析结果。\n\n## 任务目标\n{s['goal']}\n\n## 读取的输入\n{rows}\n\n## 实际完成\n校验输入文件、记录文件指纹、生成当前产物。\n\n## 交付约束\n下一步骤使用独立会话，只读取交接包中列出的文件。\n\n## 待接入\n该步骤的真实模型能力尚未连接。\n"
            safe_path(w, output).write_text(content, encoding="utf8")
            st["status"] = "done"
            st["output"] = output
            return {"detail": f"已校验 {len(inputs)} 个输入并生成 output.md。执行器：本地演示。", "file": output, "model": st["model"], "mode": "demo"}
        return self.commit(pid, "完成本地步骤", "completed", act, expected)

    def handoff(self, pid, expected):
        def act(s, w, previous):
            i = s["active"]
            if i >= len(s["stages"]) - 1:
                raise ValueError("已到最后一步，可导出交付包")
            st, nxt = s["stages"][i:i+2]
            if st["status"] != "done":
                raise ValueError("请先完成当前步骤")
            source_files = [p for p in (w / "assets").rglob("*") if p.is_file()]
            source_files.append(safe_path(w, st["output"]))
            if (w / st["folder"] / "input" / "handoff.json").exists():
                source_files.append(w / st["folder"] / "input" / "handoff.json")
            package = []
            for p in source_files:
                rel = p.relative_to(w).as_posix()
                target = f"{nxt['folder']}/input/files/{rel}"
                to = safe_path(w, target)
                to.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, to)
                if digest(p) != digest(to):
                    raise ValueError("文件复制校验失败，请重试交接")
                package.append({"source": rel, "target": target, "sha256": digest(to), "bytes": to.stat().st_size})
            manifest = {"schema": "lancer-handoff/v1", "project": pid, "sourceRevision": previous["revision"], "targetRevision": s["revision"], "from": st["model"], "to": nxt["model"], "goal": s["goal"], "cwd": nxt["folder"], "files": package, "decisions": ["交接文件与任务目标；不复制聊天历史"], "openQuestions": ["真实模型执行器尚未接入"], "mode": "demo", "createdAt": stamp()}
            hp = f"{nxt['folder']}/input/handoff.json"
            safe_path(w, hp).write_text(dumps(manifest), encoding="utf8")
            nxt["status"], nxt["session"] = "active", uid()
            s["active"] += 1
            return {"detail": f"{st['model']} → {nxt['model']} · 已复制 {len(package)} 个文件并校验 SHA-256。下一阶段使用新的会话 ID。", "file": hp, "model": nxt["model"], "fromStage": i, "sourceRevision": previous["revision"]}
        return self.commit(pid, "文件交接", "handoff", act, expected)

    def save_file(self, pid, path, data, expected, imported=False):
        if len(data) > MAX_FILE:
            raise ValueError("单个文件最多 20 MB")
        def act(s, w, previous):
            dest = safe_path(w, path)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            # Editing/importing while a stage is complete requires rerunning that stage.
            st = s["stages"][s["active"]]
            if st["status"] == "done":
                st["status"] = "active"
            return {"detail": f"{path} · {len(data)} bytes。已保存独立版本，可随时恢复。", "file": path}
        return self.commit(pid, "导入文件" if imported else "保存文件", "file", act, expected)

    def restore(self, pid, rid, expected):
        self.revision_dir(pid, rid)
        def act(s, w, previous):
            # Reconcile relative handoff paths to the new revision; original provenance remains.
            return {"detail": f"已恢复版本 {rid} 的全部文件和步骤状态。原当前版本 {previous['revision']} 仍保留。", "sourceRevision": rid}
        return self.commit(pid, "恢复历史版本", "restore", act, expected, source=rid)

    def checkpoint(self, pid, name, expected):
        return self.commit(pid, name[:100] or "手动检查点", "checkpoint", lambda s,w,p: {"detail":"文件和任务状态一起保存。"}, expected)

    def model(self, pid, model, expected):
        if model not in MODELS:
            raise ValueError("未知执行器")
        def act(s,w,p):
            st=s["stages"][s["active"]]
            st["model"]=model
            return {"detail": f"当前步骤分配给 {model}。这里只修改流程配置，未连接外部服务。"}
        return self.commit(pid, "调整执行器", "config", act, expected)

    def export(self, pid):
        with self.lock:
            s,w=self.current(pid)
            buff=io.BytesIO()
            with zipfile.ZipFile(buff,"w",zipfile.ZIP_DEFLATED) as z:
                for x in inventory(w):
                    z.write(w/x["path"], "workspace/"+x["path"])
                z.writestr("project.json",dumps(s))
                z.writestr("README.txt", f"yuki雪站 本地交付包。模式：{s.get('mode','demo')}。真实执行情况请查看 project.json 和 receipts。文件为实际保存的当前版本。\n")
            return buff.getvalue()

class Conflict(ValueError):
    pass

def shutdown_background_workers(studio, comparisons, controlled, live=None):
    """Cancel accepted work and wait for all owners, including their pools.

    Call only after HTTP request handlers have drained, so they cannot accept
    another job. Recheck under owner locks: a comparison may have passed its
    cancel check and be about to start a session when shutdown begins. There is
    no unsafe timeout that releases ownership while a worker can still write.
    """
    with studio.lock:
        comparisons.cancel.set()
        controlled.cancel.set()
        for cancel in studio.cancels.values(): cancel.set()
    if hasattr(studio, 'triggers'):
        studio.triggers.close()
    if hasattr(studio, 'shutdown_admission'):
        studio.shutdown_admission()
    if hasattr(studio, 'capacity'):
        studio.capacity.close()
    while True:
        with studio.lock:
            comparisons.cancel.set()
            controlled.cancel.set()
            for cancel in studio.cancels.values():
                cancel.set()
            workers={worker for worker in
                     [*studio.threads.values(),comparisons.thread,controlled.thread]
                     if worker is not None and worker.is_alive()}
        if live is not None:
            with live.lock:
                live.cancel.set()
                if live.thread is not None and live.thread.is_alive():
                    workers.add(live.thread)
        # Keep a parent seen alive under its owner lock in this round even if
        # it has since exited. It may have spawned a session after our snapshot
        # and failed before joining it; the next pass must discover that child.
        if not workers:
            if hasattr(studio, 'task_dispatcher'): studio.task_dispatcher.join()
            return
        for worker in workers:
            worker.join(timeout=0.1)

def make_handler(store, token, live=None):
    from studio import Studio
    from collaboration_comparison import ComparisonRunner
    from controlled_experiment import ControlledExperimentRunner
    studio=Studio(store)
    comparisons=ComparisonRunner(studio)
    controlled=ControlledExperimentRunner(studio)
    class Handler(BaseHTTPRequestHandler):
        @staticmethod
        def shutdown_background():
            shutdown_background_workers(studio,comparisons,controlled,live)

        def log_message(self,*args):
            pass

        def send(self, status, data, mime="application/json; charset=utf-8", filename=None):
            if not isinstance(data,bytes):
                data=(dumps(data) if mime.startswith("application/json") else data).encode("utf8")
            self.send_response(status)
            self.send_header("Content-Type",mime)
            self.send_header("Content-Length",str(len(data)))
            self.send_header("Cache-Control","no-store")
            self.send_header("X-Content-Type-Options","nosniff")
            self.send_header("Referrer-Policy","no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            if filename:
                self.send_header("Content-Disposition",f"attachment; filename*=UTF-8''{quote(filename)}")
            self.end_headers()
            self.wfile.write(data)

        def boundary(self, write=False):
            host=self.headers.get("Host","")
            if host not in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}:
                raise PermissionError("仅接受本机访问")
            origin=self.headers.get("Origin")
            if origin and origin not in {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}:
                raise PermissionError("不接受其他网页发起的请求")
            if write and not secrets.compare_digest(self.headers.get("X-Lancer-Token",""),token):
                raise PermissionError("请从本地工作台发起操作")

        def do_GET(self):
            try:
                self.boundary()
                url=urlparse(self.path)
                q=parse_qs(url.query)
                get=lambda k:q.get(k,[""])[0]
                if url.path=="/api/desktop/status":
                    from runtime_versions import source_matches, build_reference
                    return self.send(200,{'schema':'yuki-desktop-service/v1',
                        'dataDir':str(store.root.resolve()),'pid':os.getpid(),
                        'runtimeBuild':build_reference(studio.runtime_build),
                        'sourceMatchesRuntimeBuild':source_matches(studio.runtime_build)})
                if url.path=="/api/studio":
                    return self.send(200,studio.index())
                if url.path=="/api/studio/triggers/status":
                    return self.send(200,studio.triggers.status())
                if url.path=="/api/studio/triggers":
                    return self.send(200,studio.triggers.snapshot(get("project")))
                if url.path=="/api/studio/queue":
                    return self.send(200,studio.queue_snapshot())
                if url.path=="/api/studio/project":
                    return self.send(200,studio.project(get('project')))
                if url.path=="/api/studio/comparisons":
                    return self.send(200,comparisons.index(get('project')))
                if url.path=="/api/studio/controlled-experiments":
                    return self.send(200,controlled.index(get('project')))
                if url.path=="/api/studio/controlled-export":
                    return self.send(200,controlled.export(get('project'),get('id')),"application/zip","yuki-controlled-experiment.zip")
                if url.path=="/api/studio/session":
                    return self.send(200,studio.load(get('project'),get('session')))
                if url.path=="/api/studio/context":
                    return self.send(200,studio.context(get('project'),get('session'),get('ref')))
                if url.path=="/api/studio/export":
                    return self.send(200,studio.export(get('project'),get('session')),"application/zip","yuki-xuezhan-project-session.zip")
                if url.path=="/api/live":
                    return self.send(200,live.view(int(get("after") or 0)))
                if url.path=="/api/live/asset":
                    data,mime=live.asset(get("path"))
                    return self.send(200,data,mime)
                if url.path=="/api/projects":
                    return self.send(200,{"projects":store.list_projects(),"mode":"demo"})
                if url.path=="/api/project":
                    return self.send(200,store.view(get("id")))
                if url.path=="/api/file":
                    with store.lock:
                        s,w=store.current(get("id"))
                        p=safe_path(w,get("path"))
                        if not p.is_file():
                            raise ValueError("文件不存在")
                        b=p.read_bytes()
                    if get("download")=="1":
                        return self.send(200,b,"application/octet-stream",p.name)
                    try:
                        content=b.decode("utf8")
                        if "\x00" in content or len(b)>1024*1024:
                            raise UnicodeError()
                    except UnicodeError:
                        return self.send(200,{"binary":True,"size":len(b),"path":get("path"),"revision":s["revision"]})
                    return self.send(200,{"content":content,"binary":False,"path":get("path"),"revision":s["revision"]})
                if url.path=="/api/export":
                    return self.send(200,store.export(get("id")),"application/zip","yuki-xuezhan-workspace.zip")
                if url.path.startswith('/icons/'):
                    name=url.path.removeprefix('/icons/') if hasattr(str,'removeprefix') else url.path[len('/icons/'):]
                    if name not in ('claude-color.svg','deepseek-color.svg','gemini-color.svg','openai.svg'):
                        return self.send(404,{'error':'图标不存在'})
                    return self.send(200,(HERE/'public'/'icons'/name).read_bytes(),'image/svg+xml')
                mapping={"/":"index.html","/index.html":"index.html","/app.js":"app.js","/motion.js":"motion.js","/comments.html":"comments.html","/comments.js":"comments.js","/style.css":"style.css","/research.html":"research.html","/live":"live.html","/live.html":"live.html","/live.js":"live.js","/live.css":"live.css","/flow.js":"flow.js","/studio":"studio.html","/studio.js":"studio.js","/studio.css":"studio.css","/studio-motion.js":"studio-motion.js","/studio-tree.js":"studio-tree.js","/studio-tree.css":"studio-tree.css","/studio-replay.js":"studio-replay.js"}
                if url.path not in mapping:
                    return self.send(404,{"error":"不存在的页面"})
                path=HERE/"public"/mapping[url.path]
                data=path.read_bytes().replace(b"__LOCAL_TOKEN__",token.encode()) if path.name in ("index.html","live.html","studio.html") else path.read_bytes()
                mime={".html":"text/html; charset=utf-8",".js":"text/javascript; charset=utf-8",".css":"text/css; charset=utf-8"}[path.suffix]
                self.send(200,data,mime)
            except PermissionError as e:
                self.send(403,{"error":str(e)})
            except (ValueError,OSError) as e:
                self.send(400,{"error":str(e)})

        def do_POST(self):
            try:
                self.boundary(True)
                length=int(self.headers.get("Content-Length","0"))
                if length<0 or length>MAX_FILE*1.4+10000:
                    raise ValueError("请求太大")
                if not self.headers.get("Content-Type","").startswith("application/json"):
                    raise ValueError("仅支持 JSON 请求")
                body=json.loads(self.rfile.read(length))
                if not isinstance(body,dict):
                    raise ValueError("无效请求")
                if self.path.startswith('/api/studio/'):
                    project=body.get('project');session=body.get('session');route=self.path[len('/api/studio/'):]
                    if route=='triggers/source/save':result=studio.triggers.save_source(body.get('source'))
                    elif route=='triggers/source/resume':result=studio.triggers.resume_source(body.get('sourceId'),body.get('revision'))
                    elif route=='triggers/github/connect':result=studio.triggers.connect_github(body.get('sourceId'),body.get('token'),body.get('useGitCredential',False))
                    elif route=='triggers/github/disconnect':result=studio.triggers.disconnect_github(body.get('sourceId'))
                    elif route=='triggers/poll':result=studio.triggers.request_poll(body.get('sourceId'))
                    elif route=='triggers/evaluate':result=studio.triggers.request_evaluate(body.get('candidateId'))
                    elif route=='triggers/dispatch':result=studio.triggers.request_dispatch(body.get('candidateId'))
                    elif route=='triggers/subscription/update':result=studio.triggers.update_subscription(body.get('sourceId'),body.get('threadKey'),body.get('revision'),body.get('origin'),body.get('version'),body.get('action'))
                    elif route=='triggers/subscription/claim':result=studio.triggers.subscribe_thread(body.get('sourceId'),body.get('origin'),body.get('threadKey'),body.get('container'),body.get('revision'))
                    elif route=='triggers/custom':result=studio.triggers.ingest_custom(body.get('sourceId'),body.get('payload'))
                    elif route=='triggers/publish':result=studio.triggers.request_publish(body.get('effectId'))
                    elif route=='triggers/reconcile':
                        studio.triggers.engine.reconcile(body.get('effectId'));result=studio.triggers.snapshot(project)
                    elif route=='project/create':result=studio.create_project(body.get('title'),body.get('goal',''),body.get('instructions',''))
                    elif route=='project/update':result=studio.update_project(project,body.get('instructions',''),body.get('version'),body.get('teamConfig'))
                    elif route=='project/role':result=studio.save_custom_role(project,body.get('roleId'),body.get('definition'),body.get('version'))
                    elif route=='project/import':
                        if live and live.owns_active(project):raise Conflict('原工作台正在执行此项目，请先等待完成')
                        result=studio.import_files(project,body.get('files'),body.get('revision'))
                    elif route=='session/create':result=studio.create_session(project,body.get('title',''),body.get('text',''),body.get('kind','auto'))
                    elif route=='session/message':result=studio.add_message(project,session,body.get('text',''),body.get('version'),body.get('kind','auto'))
                    elif route=='session/start':result=studio.start(project,session,body.get('mode','live'),body.get('resume',False),body.get('coordinatorModel'),body.get('teamModels'),task_route=body.get('taskRoute'))
                    elif route=='session/evidence-review':
                        from evidence_review import start_evidence_review
                        result=start_evidence_review(studio,project,session,body.get('version'))
                    elif route=='session/team':result=studio.set_team(project,session,body.get('coordinatorModel'),body.get('teamModels'),body.get('version'))
                    elif route=='models/register':result=studio.register_model(body)
                    elif route=='models/discover':result=studio.discover_models(body.get('provider'))
                    elif route=='models/policy':result=studio.update_model_policy(body.get('modelId'),body.get('requestPolicy'),body.get('version'))
                    elif route=='models/probe':result=studio.probe_model(body.get('modelId'))
                    elif route=='models/probe/stop':result=studio.stop_model_probe(body.get('modelId'))
                    elif route=='host-acceptance/register':result=studio.register_host_acceptance(body)
                    elif route=='host-acceptance/remove':result=studio.remove_host_acceptance(body.get('id'))
                    elif route=='project/host-acceptance':result=studio.set_host_acceptance(project,body.get('version'),body.get('checkIds'))
                    elif route=='mcp/save':studio.mcp.save(body);result=studio.index()
                    elif route=='mcp/probe':studio.mcp.probe(body.get('id'));result=studio.index()
                    elif route=='mcp/remove':studio.mcp.remove(body.get('id'));result=studio.index()
                    elif route=='work-policy':
                        from work_policy import update_policy
                        result=update_policy(studio,body)
                    elif route=='comparison/start':result=comparisons.start(project,body.get('coordinatorModel'),body.get('teamModels'),body.get('caseId','order-fulfillment-v1'),body.get('repetitions',1))
                    elif route=='controlled-experiment/start':result=controlled.start(project,body.get('coordinatorModel'),body.get('teamModels'),body.get('caseId','order-fulfillment-v1'),body.get('budget'),body.get('repetitions',1),body.get('homogeneous',True))
                    elif route=='controlled-experiment/stop':result=controlled.stop(project,body.get('id'))
                    elif route=='comparison/stop':result=comparisons.stop(project,body.get('id'))
                    elif route=='contribution/verify':
                        from contribution_tracking import verify_contribution
                        result=verify_contribution(studio,project,session,body)
                    elif route=='session/route':result=studio.set_task_route_selection(project,session,body.get('taskRoute'),body.get('version'))
                    elif route=='session/stop':result=studio.stop(project,session)
                    elif route=='example':result=studio.example(bool(body.get('fresh')))
                    elif route=='business-example':result=studio.business_example(bool(body.get('fresh')))
                    elif route=='connect':studio.client.configure(body.get('provider'),body.get('key'));result={'available':studio.client.available()}
                    elif route=='native':result={'native':studio.client.configure_native(body.get('enabled')),'available':studio.client.available()}
                    elif route=='forget':studio.client.forget();result={'available':[]}
                    else:return self.send(404,{'error':'未知项目操作'})
                    return self.send(200,result)
                if self.path=="/api/live/scenario":
                    return self.send(200,live.start_scenario())
                if self.path=="/api/live/select":
                    return self.send(200,live.select_run(body.get("runId")))
                if self.path=="/api/live/start":
                    return self.send(200,live.start())
                if self.path=="/api/live/continue":
                    return self.send(200,live.resume())
                if self.path=="/api/live/repair":
                    return self.send(200,live.repair())
                if self.path=="/api/live/stop":
                    return self.send(200,live.stop())
                if self.path=="/api/live/forget":
                    return self.send(200,live.forget())
                if self.path=="/api/live/plan":
                    return self.send(200,live.configure(body))
                if self.path=="/api/live/note":
                    return self.send(200,live.add_note(body))
                pid=body.get("id")
                if live and live.owns_active(pid):
                    raise Conflict("真实流程正在执行；请先停止流程再修改或恢复工作区。")
                expected=body.get("revision")
                route=urlparse(self.path).path
                if route=="/api/create":
                    pid=store.create(body.get("title"),body.get("goal",""))
                    return self.send(201,store.view(pid))
                if not isinstance(expected,str) or not expected:
                    raise ValueError("需要工作区版本号，请刷新页面")
                if route=="/api/complete":
                    result=store.complete(pid,expected)
                elif route=="/api/handoff":
                    result=store.handoff(pid,expected)
                elif route=="/api/restore":
                    result=store.restore(pid,body.get("target"),expected)
                elif route=="/api/checkpoint":
                    result=store.checkpoint(pid,str(body.get("name","手动检查点")),expected)
                elif route=="/api/model":
                    result=store.model(pid,body.get("model"),expected)
                elif route=="/api/save":
                    content=body.get("content")
                    if not isinstance(content,str):
                        raise ValueError("文件内容需为文本")
                    result=store.save_file(pid,body.get("path"),content.encode("utf8"),expected)
                elif route=="/api/upload":
                    name=body.get("name","")
                    if not name or len(name)>180 or Path(name).name!=name or name in (".","..") or "\\" in name:
                        raise ValueError("无效文件名")
                    data=base64.b64decode(body.get("data",""),validate=True)
                    result=store.save_file(pid,"assets/"+name,data,expected,True)
                else:
                    return self.send(404,{"error":"未知操作"})
                self.send(200,result)
            except Conflict as e:
                self.send(409,{"error":str(e)})
            except PermissionError as e:
                self.send(403,{"error":str(e)})
            except (ValueError,TypeError,OSError,KeyError) as e:
                self.send(400,{"error":str(e)})
    return Handler

def main(credentials=None):
    parser=argparse.ArgumentParser()
    parser.add_argument("--port",type=int,default=8767)
    parser.add_argument("--data-dir",type=Path,default=HERE/".lancer-data")
    parser.add_argument("--open",action="store_true")
    args=parser.parse_args()
    # Claim the port before Studio initialization can recover running sessions.
    server=ThreadingHTTPServer(("127.0.0.1",args.port),BaseHTTPRequestHandler)
    # server_close must drain accepted requests before stopping their workers.
    server.daemon_threads=False
    data_lock=None
    try:
        # A different port must not allow another process to recover or write
        # this server's live sessions. Hold the directory before any Store or
        # handler initialization, and keep it until the listener is closed.
        data_lock=ServiceDataLock(args.data_dir)
        data_lock.acquire()
        store=Store(args.data_dir)
        from orchestrator import Orchestrator
        live=Orchestrator(store,credentials or {})
        server.RequestHandlerClass=make_handler(store,secrets.token_urlsafe(32),live)
        url=f"http://127.0.0.1:{server.server_port}"
        print(f"yuki雪站: {url}/studio\n数据目录: {args.data_dir.resolve()}\n会话内连接: {len(credentials or {})} 个（密钥不落盘）",flush=True)
        if args.open:
            webbrowser.open(url+'/studio')
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        shutdown_complete=False
        try:
            server.server_close()
            shutdown=getattr(server.RequestHandlerClass,'shutdown_background',None)
            if shutdown is not None:
                shutdown()
            shutdown_complete=True
        finally:
            if data_lock is not None and shutdown_complete:
                data_lock.release()
            # If shutdown itself is interrupted/failed, intentionally retain
            # the descriptor until process exit. Releasing it could admit a
            # second writer while an accepted worker is still finishing.

if __name__=="__main__":
    # Helpers import app; keep exception/state identities shared with this
    # script entry instead of loading a second copy as the app module.
    import sys
    sys.modules['app'] = sys.modules[__name__]
    main()
