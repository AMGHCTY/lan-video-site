#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
局域网视频站 —— 电脑上启动，手机浏览器打开同一网络下的地址即可在线播放视频。

用法:
    python video_server.py                     # 默认播放脚本旁的「视频」文件夹
    python video_server.py --dir D:\\电影       # 启动时添加目录（可多次使用）
    python video_server.py --port 9000         # 指定端口（默认 8080，被占用自动往后找）

添加目录的三种方式：
    1. 网页首页点「＋ 添加目录」
    2. 启动后在本控制台窗口直接粘贴文件夹路径回车（输入 help 看全部命令）
    3. 启动参数 --dir
目录列表保存在 library.json，重启仍生效。

播放原理：HTTP Range 流式传输；对 moov 索引在文件尾部的 mp4，服务器在内存里
实时把字节流虚拟重排为索引在前的顺序（不落盘、不转码、不改原文件），
手机端因此可以任意秒拖进度条，也不占用任何硬盘缓存。仅依赖 Python 标准库。
"""

import argparse
import ctypes
import hashlib
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.parse
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 支持列出的视频扩展名（浏览器能否直接播放取决于编码，mp4/webm 兼容性最好）
VIDEO_EXTS = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
    ".webm": "video/webm", ".mkv": "video/x-matroska", ".avi": "video/x-msvideo",
    ".flv": "video/x-flv", ".wmv": "video/x-ms-wmv", ".ts": "video/mp2t",
    ".3gp": "video/3gpp", ".ogv": "video/ogg",
}
FASTSTART_EXTS = {".mp4", ".m4v", ".mov"}   # 可虚拟重排索引的容器
CHUNK = 1024 * 1024                          # 流式传输块大小 1MB
SEARCH_LIMIT = 300

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "library.json")
THUMB_DIR = os.path.join(SCRIPT_DIR, ".thumbcache")   # 缩略图磁盘缓存
FAV_FILE = os.path.join(SCRIPT_DIR, "favorites.json")  # 收藏夹数据


# ================================================================ 视频库（多目录）

class Library:
    """所有可播放的根目录。路径统一为 `<别名>/子目录/文件.mp4` 的形式。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.dirs = []   # [{"alias": 显示名, "path": 绝对路径}]

    @staticmethod
    def _basename(path):
        return os.path.basename(os.path.normpath(path)) or path

    def load(self, cli_dirs):
        loaded = []
        if os.path.isfile(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, encoding="utf-8") as f:
                    data = json.load(f)
                for d in data.get("dirs", []):
                    # 目录暂时不可用（如 U 盘未插）也保留，重新挂载后自动恢复
                    if isinstance(d, dict) and isinstance(d.get("path"), str):
                        loaded.append({
                            "alias": str(d.get("alias") or self._basename(d["path"])),
                            "path": os.path.abspath(d["path"]),
                        })
            except (OSError, ValueError):
                loaded = []
        if not loaded:  # 首次运行：默认添加脚本旁的「视频」文件夹
            default = os.path.join(SCRIPT_DIR, "视频")
            if os.path.isdir(default):
                loaded.append({"alias": "视频", "path": default})

        with self.lock:
            seen, self.dirs = set(), []
            for d in loaded:  # 按路径去重（Windows 不区分大小写）
                key = os.path.normcase(d["path"])
                if key in seen:
                    continue
                seen.add(key)
                self.dirs.append(d)
            taken = set()
            for d in self.dirs:  # 保证别名唯一
                alias = d["alias"]
                if alias in taken:
                    i = 2
                    while f"{alias}-{i}" in taken:
                        i += 1
                    alias = f"{alias}-{i}"
                    d["alias"] = alias
                taken.add(alias)

        for raw in cli_dirs or []:
            p = os.path.abspath(raw.strip().strip('"').strip("'"))
            if not os.path.isdir(p):
                print(f"  ⚠ 启动参数里的目录不存在，已跳过：{p}", flush=True)
                continue
            try:
                self.add(p)
            except ValueError:
                pass
        self.save()

    def save(self):
        with self.lock:
            snapshot = [dict(d) for d in self.dirs]
        tmp = CONFIG_FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"dirs": snapshot}, f, ensure_ascii=False, indent=2)
            os.replace(tmp, CONFIG_FILE)
        except OSError as e:
            print(f"  ⚠ 保存 library.json 失败：{e}", flush=True)

    def add(self, path):
        path = os.path.abspath(path)
        with self.lock:
            for d in self.dirs:
                if os.path.normcase(d["path"]) == os.path.normcase(path):
                    raise ValueError("该目录已在列表中")
            base = self._basename(path)
            taken = {d["alias"] for d in self.dirs}
            alias, i = base, 2
            while alias in taken:
                alias = f"{base}-{i}"
                i += 1
            entry = {"alias": alias, "path": path}
            self.dirs.append(entry)
        return entry

    def remove(self, alias):
        with self.lock:
            n = len(self.dirs)
            self.dirs = [d for d in self.dirs if d["alias"] != alias]
            return len(self.dirs) < n

    def roots(self):
        with self.lock:
            return [dict(d) for d in self.dirs]

    def by_alias(self, alias):
        with self.lock:
            for d in self.dirs:
                if d["alias"] == alias:
                    return dict(d)
        return None


LIB = Library()


def resolve(rel: str):
    """把 `<别名>/子路径` 解析为绝对路径，并确保不越出对应根目录。"""
    rel = (rel or "").strip().replace("\\", "/")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    entry = LIB.by_alias(parts[0])
    if not entry:
        return None
    root = os.path.abspath(entry["path"])
    target = os.path.abspath(os.path.join(root, *parts[1:])) if len(parts) > 1 else root
    if target != root and not target.startswith(root + os.sep):
        return None
    return target


def thumb_key(fs_path: str) -> str:
    """缩略图缓存键：路径+大小+修改时间。文件被替换会自动换新键重新生成。"""
    st = os.stat(fs_path)
    basis = f"{os.path.abspath(fs_path).lower()}|{st.st_size}|{int(st.st_mtime)}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()


def is_video(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in VIDEO_EXTS


def is_removable_drive(fs_path: str) -> bool:
    """Windows 可移动磁盘（U 盘）：每次重新插拔后文件的扫描缓存会失效。"""
    try:
        drive = os.path.splitdrive(os.path.abspath(fs_path))[0] + "\\"
        return ctypes.windll.kernel32.GetDriveTypeW(drive) == 2   # DRIVE_REMOVABLE
    except Exception:
        return False


_COUNT_CACHE = {}   # 目录 -> (时间, 视频数)，避免每次导航都遍历


def count_videos(fs_dir: str, cap: int = 5000) -> int:
    now = time.time()
    hit = _COUNT_CACHE.get(fs_dir)
    if hit and now - hit[0] < 20:
        return hit[1]
    n = 0
    try:
        for dirpath, dirnames, filenames in os.walk(fs_dir):
            dirnames[:] = [d for d in dirnames if not d.startswith((".", "$"))]
            for name in filenames:
                if is_video(name):
                    n += 1
                    if n >= cap:
                        break
            if n >= cap:
                break
    except OSError:
        pass
    _COUNT_CACHE[fs_dir] = (now, n)
    return n


def list_dir(rel: str):
    """列出目录内容。rel 为空时列出所有已配置的视频根目录（含视频数量）。"""
    folders, videos = [], []
    if not (rel or "").strip().replace("\\", "/").strip("/"):
        for d in LIB.roots():
            folders.append({"name": d["alias"], "path": d["alias"], "full": d["path"],
                            "removable": True, "count": count_videos(d["path"])})
        return {"cur": "", "folders": folders, "videos": videos}

    fs_path = resolve(rel)
    if fs_path is None or not os.path.isdir(fs_path):
        return None
    rel_norm = "/".join(p for p in rel.replace("\\", "/").split("/") if p and p != ".")
    try:
        names = os.listdir(fs_path)
    except OSError:
        names = []
    for name in sorted(names, key=str.lower):
        if name.startswith((".", "$")):
            continue
        full = os.path.join(fs_path, name)
        child_rel = f"{rel_norm}/{name}"
        try:
            st = os.stat(full)
        except OSError:
            continue
        if os.path.isdir(full):
            folders.append({"name": name, "path": child_rel, "count": count_videos(full)})
        elif os.path.isfile(full) and is_video(name):
            videos.append({"name": name, "path": child_rel,
                           "size": st.st_size, "mtime": int(st.st_mtime)})
    return {"cur": rel_norm, "folders": folders, "videos": videos}


def search(q: str):
    """在所有目录中按文件名搜索视频。"""
    q = q.lower().strip()
    results = []
    for entry in LIB.roots():
        alias, root = entry["alias"], entry["path"]
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith((".", "$"))]
            for name in filenames:
                if not is_video(name) or q not in name.lower():
                    continue
                full = os.path.join(dirpath, name)
                rel = f"{alias}/{os.path.relpath(full, root).replace(chr(92), '/')}"
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                results.append({"name": name, "path": rel,
                                "folder": os.path.dirname(rel) or alias,
                                "size": st.st_size, "mtime": int(st.st_mtime)})
                if len(results) >= SEARCH_LIMIT:
                    return results
    return results


# ================================================================ 虚拟 faststart 流（零缓存）

class Faststart:
    """moov 在尾部的 mp4 的虚拟重排视图：字节总长不变，moov 挪到最前，
    mdat 内的块偏移表（stco/co64）整体加上 moov 大小的位移。不生成任何文件。"""
    __slots__ = ("size", "segs", "moov_buf")
    # segs: [(虚拟起始, 长度, 源偏移或 None)]；None 表示这段来自 moov_buf


CONTAINER_BOXES = {"moov", "trak", "mdia", "minf", "stbl"}


def _parse_top_boxes(f, filesize):
    boxes = []
    pos = 0
    for _ in range(64):
        f.seek(pos)
        head = f.read(16)
        if len(head) < 8:
            break
        size = int.from_bytes(head[0:4], "big")
        btype = head[4:8].decode("latin-1")
        hdr = 8
        if size == 1:
            if len(head) < 16:
                break
            size = int.from_bytes(head[8:16], "big")
            hdr = 16
        elif size == 0:                       # 到文件尾
            boxes.append((btype, pos, filesize - pos))
            break
        if size < hdr:
            break
        boxes.append((btype, pos, size))
        pos += size
        if pos >= filesize:
            break
    return boxes


def _collect_stco(buf, base, out):
    """在 moov 里递归找出所有 stco/co64 的条目区位置（相对 moov 开头）。"""
    n = len(buf)
    pos = 0
    for _ in range(256):
        if pos + 8 > n:
            return
        size = int.from_bytes(buf[pos:pos + 4], "big")
        btype = buf[pos + 4:pos + 8].decode("latin-1", "replace")
        hdr = 8
        if size == 1:
            if pos + 16 > n:
                return
            size = int.from_bytes(buf[pos + 8:pos + 16], "big")
            hdr = 16
        if size < hdr:
            return
        if btype in CONTAINER_BOXES:
            _collect_stco(buf[pos + hdr:pos + size], base + pos + hdr, out)
        elif btype in ("stco", "co64"):
            p = pos + hdr + 4                 # 跳过 version/flags
            if p + 4 > n:
                return
            cnt = int.from_bytes(buf[p:p + 4], "big")
            if cnt * (8 if btype == "co64" else 4) + p + 4 > n:
                return
            out.append((base + p + 4, btype == "co64", cnt))
        pos += size
        if pos >= n:
            return


def build_faststart(src: str, st):
    try:
        with open(src, "rb") as f:
            boxes = _parse_top_boxes(f, st.st_size)
            names = [b[0] for b in boxes]
            if "mdat" not in names or "moov" not in names:
                return None
            first_mdat = next(i for i, b in enumerate(boxes) if b[0] == "mdat")
            if "moov" in names[:first_mdat]:
                return None                   # 已经是 faststart，无需处理
            moov = next(b for b in boxes if b[0] == "moov")
            if moov[2] > 64 * 1024 * 1024:    # 超大索引不进内存，直接原样播放
                return None
            f.seek(moov[1])
            moov_buf = bytearray(f.read(moov[2]))
    except OSError:
        return None

    stco_list = []
    _collect_stco(bytes(moov_buf), 0, stco_list)
    delta = moov[2]                           # mdat 整体后移 moov 大小
    for epos, is64, cnt in stco_list:
        width = 8 if is64 else 4
        for i in range(cnt):
            p = epos + i * width
            val = int.from_bytes(moov_buf[p:p + width], "big")
            if not is64 and val + delta > 0xFFFFFFFF:
                return None                   # 32 位偏移溢出，放弃重排
            moov_buf[p:p + width] = (val + delta).to_bytes(width, "big")

    # 虚拟布局：保持原顺序，仅把 moov 移到 ftyp 之后
    new_order = [b for b in boxes if b is not moov]
    insert_at = 1 if (new_order and new_order[0][0] == "ftyp") else 0
    new_order.insert(insert_at, moov)

    segs, virt = [], 0
    for b in new_order:
        segs.append((virt, b[2], None if b is moov else b[1]))
        virt += b[2]
    if virt != st.st_size:
        return None                           # 布局与原文件不一致，保险起见放弃

    fs = Faststart()
    fs.size = st.st_size
    fs.segs = segs
    fs.moov_buf = bytes(moov_buf)
    return fs


_FS_CACHE = {}      # (路径, mtime, size) -> Faststart 或 None
_FS_ORDER = []
_FS_GUARD = threading.Lock()
_FS_BUILD_LOCKS = {}


def get_faststart(src: str):
    try:
        st = os.stat(src)
    except OSError:
        return None
    key = (os.path.abspath(src), st.st_mtime, st.st_size)
    with _FS_GUARD:
        if key in _FS_CACHE:
            return _FS_CACHE[key]
    lk = _FS_BUILD_LOCKS.setdefault(key[0], threading.Lock())
    with lk:
        with _FS_GUARD:
            if key in _FS_CACHE:
                return _FS_CACHE[key]
        fs = build_faststart(src, st)
        with _FS_GUARD:
            _FS_CACHE[key] = fs
            _FS_ORDER.append(key)
            while len(_FS_ORDER) > 8:         # 只保留最近用到的几部片的重排信息
                old = _FS_ORDER.pop(0)
                _FS_CACHE.pop(old, None)
        return fs


def iter_faststart_data(fs: Faststart, f, start: int, length: int):
    """按虚拟布局输出 [start, start+length) 的字节，跨段自动拼接。"""
    remaining, vpos = length, start
    for v0, ln, src_off in fs.segs:
        if remaining <= 0:
            break
        vend = v0 + ln
        if vend <= vpos:
            continue
        if v0 >= vpos + remaining:
            break
        s = max(vpos, v0) - v0
        take = min(vend, vpos + remaining) - max(vpos, v0)
        if src_off is None:
            yield fs.moov_buf[s:s + take]
        else:
            f.seek(src_off + s)
            data = f.read(take)
            if len(data) < take:
                return
            yield data
        remaining -= take
        vpos += take


# ================================================================ 收藏夹

class FavStore:
    """收藏夹：folders 为收藏夹列表，items 为收藏的视频（引用相对路径）。
    视频改名时路径自动迁移；源文件删除后在浏览时自动清理。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.folders = []   # [{"id","name","created"}]
        self.items = []     # [{"fid","path","name","size","mtime","added"}]

    def load(self):
        data = None
        if os.path.isfile(FAV_FILE):
            try:
                with open(FAV_FILE, encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = None
        if isinstance(data, dict):
            self.folders = [d for d in data.get("folders", [])
                            if isinstance(d, dict) and d.get("id") and d.get("name")]
            self.items = [i for i in data.get("items", []) if isinstance(i, dict)]
        if not any(d["id"] == "default" for d in self.folders):
            self.folders.insert(0, {"id": "default", "name": "默认收藏夹",
                                    "created": int(time.time())})
        self.save()

    def save(self):
        with self.lock:
            snapshot = {"folders": [dict(d) for d in self.folders],
                        "items": [dict(i) for i in self.items]}
        tmp = FAV_FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=2)
            os.replace(tmp, FAV_FILE)
        except OSError as e:
            print(f"  ⚠ 保存 favorites.json 失败：{e}", flush=True)

    def folders_with_counts(self):
        with self.lock:
            counts = {}
            for i in self.items:
                counts[i["fid"]] = counts.get(i["fid"], 0) + 1
            return [{"id": d["id"], "name": d["name"],
                     "count": counts.get(d["id"], 0),
                     "default": d["id"] == "default"} for d in self.folders]

    def folder_info(self, fid):
        with self.lock:
            for d in self.folders:
                if d["id"] == fid:
                    return {"id": d["id"], "name": d["name"], "default": d["id"] == "default"}
        return None

    def add(self, rel: str, fid: str):
        with self.lock:
            if not any(d["id"] == fid for d in self.folders):
                return False
            for i in self.items:
                if i["fid"] == fid and i["path"] == rel:
                    return True
            fs = resolve(rel)
            if fs is None or not os.path.isfile(fs):
                return False
            st = os.stat(fs)
            self.items.append({"fid": fid, "path": rel,
                               "name": os.path.basename(rel.replace("/", os.sep)),
                               "size": st.st_size, "mtime": int(st.st_mtime),
                               "added": int(time.time())})
        self.save()
        return True

    def remove(self, rel: str, fid: str):
        with self.lock:
            n = len(self.items)
            self.items = [i for i in self.items if not (i["fid"] == fid and i["path"] == rel)]
            changed = len(self.items) < n
        if changed:
            self.save()
        return changed

    def new_folder(self, name: str):
        name = name.strip()[:30]
        if not name:
            return None
        with self.lock:
            fid = "f" + uuid.uuid4().hex[:8]
            entry = {"id": fid, "name": name, "created": int(time.time())}
            self.folders.append(entry)
        self.save()
        return {"id": fid, "name": name, "count": 0, "default": False}

    def del_folder(self, fid: str):
        if fid == "default":
            return False
        with self.lock:
            self.folders = [d for d in self.folders if d["id"] != fid]
            self.items = [i for i in self.items if i["fid"] != fid]
        self.save()
        return True

    def rename_folder(self, fid: str, name: str):
        name = name.strip()[:30]
        if not name:
            return False
        with self.lock:
            for d in self.folders:
                if d["id"] == fid:
                    d["name"] = name
        self.save()
        return True

    def items_of(self, fid: str):
        """列出某收藏夹的视频；源文件已不存在的条目自动清除。"""
        out, keep, dropped = [], [], False
        with self.lock:
            for i in self.items:
                if i["fid"] != fid:
                    keep.append(i)
                    continue
                fs = resolve(i["path"])
                if fs is None or not os.path.isfile(fs):
                    dropped = True
                    continue
                keep.append(i)
                out.append(dict(i))
            if dropped:
                self.items = keep
        if dropped:
            self.save()
        return out

    def validate(self):
        """删除源文件已不存在的收藏条目。返回是否清理过。"""
        with self.lock:
            keep, dropped = [], False
            for i in self.items:
                fs = resolve(i["path"])
                if fs is None or not os.path.isfile(fs):
                    dropped = True
                    continue
                keep.append(i)
            if dropped:
                self.items = keep
        if dropped:
            self.save()
        return dropped

    def fids_for(self, rel: str):
        with self.lock:
            return [i["fid"] for i in self.items if i["path"] == rel]

    def rename_path(self, old_rel: str, new_rel: str, new_name: str):
        with self.lock:
            hit = False
            for i in self.items:
                if i["path"] == old_rel:
                    i["path"] = new_rel
                    i["name"] = new_name
                    hit = True
        if hit:
            self.save()


FAVS = FavStore()


# ================================================================ 后台预热
# Windows Defender 会对"刚拷入可移动磁盘的文件"在首次打开时做全文件扫描，
# 且 U 盘每次重新插拔后扫描缓存失效。提前在后台把每个视频顺序读一遍：
# 触发安全扫描 + 填满系统文件缓存，之后播放和缩略图都是秒开。

_WARM_SEM = threading.Semaphore(1)
WARM_FILE = os.path.join(SCRIPT_DIR, ".warmcache.json")   # 已预热文件的状态记录


def warm_video_file(fs_path: str):
    try:
        with open(fs_path, "rb") as f:
            while True:
                if not f.read(CHUNK):
                    break
    except OSError:
        pass


def warm_library(roots, label=""):
    with _WARM_SEM:   # 同时只跑一个预热任务
        state = {}
        if os.path.isfile(WARM_FILE):
            try:
                with open(WARM_FILE, encoding="utf-8") as f:
                    state = json.load(f)
            except Exception:
                state = {}
        n, total, skipped, new_state = 0, 0, 0, {}
        for entry in roots:
            removable = is_removable_drive(entry["path"])
            for dirpath, dirnames, filenames in os.walk(entry["path"]):
                dirnames[:] = [d for d in dirnames if not d.startswith((".", "$"))]
                for name in filenames:
                    if not is_video(name):
                        continue
                    p = os.path.join(dirpath, name)
                    try:
                        st = os.stat(p)
                    except OSError:
                        continue
                    key = p.lower()
                    sig = f"{int(st.st_mtime)}:{st.st_size}"
                    new_state[key] = sig
                    # 可移动磁盘每次重新挂载后扫描缓存失效，需要完整读取重新触发；
                    # 固定磁盘的扫描结果会保留，读个开头维持缓存即可
                    if state.get(key) == sig and not removable:
                        skipped += 1
                        try:
                            with open(p, "rb") as f:
                                f.read(262144)
                        except OSError:
                            pass
                        continue
                    warm_video_file(p)
                    total += st.st_size
                    n += 1
        try:
            with open(WARM_FILE, "w", encoding="utf-8") as f:
                json.dump(new_state, f, ensure_ascii=False)
        except Exception:
            pass
        prune_thumbcache()
        msg = f"{skipped} 个已缓存跳过"
        if n:
            msg += f"，完整预热 {n} 个 / {total/1048576:.0f}MB"
        print(f"  ♨ 预热完成（{label}）：{msg}", flush=True)


def prune_thumbcache():
    """清理缩略图缓存里源文件已不存在的孤儿条目。"""
    if not os.path.isdir(THUMB_DIR):
        return
    removed = 0
    for fn in os.listdir(THUMB_DIR):
        if not fn.endswith(".json"):
            continue
        fp = os.path.join(THUMB_DIR, fn)
        try:
            with open(fp, encoding="utf-8") as f:
                src = json.load(f).get("p", "")
            if src and not os.path.isfile(src):
                os.remove(fp)
                jpg = fp[:-5] + ".jpg"
                if os.path.exists(jpg):
                    os.remove(jpg)
                removed += 1
        except Exception:
            continue
    if removed:
        print(f"  ♨ 已清理 {removed} 个失效缩略图缓存", flush=True)


# ================================================================ 页面

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0a0c10">
<meta name="color-scheme" content="dark">
<title>局域网视频站</title>
<script>
(function(){
  var t = null;
  try{ t = localStorage.getItem("lan_video_theme"); }catch(e){}
  if(t !== "light" && t !== "dark"){
    t = (window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches) ? "light" : "dark";
  }
  document.documentElement.setAttribute("data-theme", t);
})();
</script>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Cdefs%3E%3ClinearGradient id='g' x1='0' y1='0' x2='1' y2='1'%3E%3Cstop offset='0' stop-color='%235b8cff'/%3E%3Cstop offset='1' stop-color='%239a6bff'/%3E%3C/linearGradient%3E%3C/defs%3E%3Crect width='64' height='64' rx='14' fill='url(%23g)'/%3E%3Cpath fill='%23fff' transform='translate(12 12) scale(1.666)' d='M18 4l2 4h-3l-2-4h-2l2 4h-3l-2-4H8l2 4H7L5 4H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V4h-4z'/%3E%3C/svg%3E">
<style>
:root{
  --bg:#0a0c10; --panel:#131822; --card:#151b28; --line:#222b3d; --line2:#2c3852;
  --text:#eef1f8; --dim:#8a93a8; --acc:#6d9bff; --acc2:#9a6bff;
  --grad:linear-gradient(135deg,#5b8cff,#9a6bff);
  --header-bg:rgba(10,12,16,.78);
  --player-bg:#07080b;
  --thumb-bg:#05070a; --thumb-icon:#3a4356;
  --dur-bg:rgba(5,7,10,.72); --dur-text:#dfe4ee;
  --back-bg:rgba(10,12,16,.5); --back-border:rgba(255,255,255,.16); --back-text:#fff;
  --glow1:rgba(91,140,255,.14); --glow2:rgba(154,107,255,.10);
  --shadow:0 10px 30px rgba(0,0,0,.4); --shadow-lg:0 20px 60px rgba(0,0,0,.5);
  --scroll-thumb:#232c40;
  --mask:rgba(3,4,7,.6);
  --dicon-bg:rgba(91,140,255,.12); --dicon-border:rgba(91,140,255,.25);
  --dx-bg:rgba(255,255,255,.05);
  --hl-text:#b9ccff; --hl-border:#31437a; --hl-bg:rgba(91,140,255,.1);
  --addchip-border:#33405e;
  color-scheme:dark;
}
html[data-theme="light"]{
  --bg:#f2f5fa; --panel:#ffffff; --card:#ffffff; --line:#e3e8f2; --line2:#c9d5ec;
  --text:#18213a; --dim:#5e6c86; --acc:#2f62f0; --acc2:#7c4dff;
  --header-bg:rgba(255,255,255,.86);
  --player-bg:#edf1f8;
  --thumb-bg:#e8edf6; --thumb-icon:#9fabc4;
  --dur-bg:rgba(255,255,255,.9); --dur-text:#2a3348;
  --back-bg:rgba(255,255,255,.78); --back-border:rgba(20,40,90,.2); --back-text:#18213a;
  --glow1:rgba(91,140,255,.10); --glow2:rgba(154,107,255,.08);
  --shadow:0 10px 30px rgba(40,70,140,.14); --shadow-lg:0 20px 60px rgba(40,70,140,.18);
  --scroll-thumb:#c9d5ec;
  --mask:rgba(25,40,80,.35);
  --dicon-bg:rgba(47,98,240,.08); --dicon-border:rgba(47,98,240,.22);
  --dx-bg:rgba(20,40,90,.05);
  --hl-text:#2f62f0; --hl-border:#bcd0ff; --hl-bg:rgba(91,140,255,.1);
  --addchip-border:#b9c8e8;
  color-scheme:light;
}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
html{color-scheme:dark}
body{background:var(--bg);color:var(--text);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  padding-bottom:env(safe-area-inset-bottom)}
body::before{content:"";position:fixed;inset:0;pointer-events:none;z-index:0;
  background:radial-gradient(1000px 420px at 18% -10%, var(--glow1), transparent 60%),
             radial-gradient(900px 420px at 90% -20%, var(--glow2), transparent 60%)}
header{position:sticky;top:0;z-index:55;background:var(--header-bg);backdrop-filter:blur(14px);
  -webkit-backdrop-filter:blur(14px);border-bottom:1px solid var(--line);padding:12px 18px}
.hrow{display:flex;align-items:center;gap:12px;max-width:1200px;margin:0 auto}
.brand{display:flex;align-items:center;gap:10px;min-width:0;cursor:pointer}
.brand .logo{width:34px;height:34px;border-radius:10px;background:var(--grad);
  display:flex;align-items:center;justify-content:center;
  box-shadow:0 4px 18px rgba(91,140,255,.35)}
.brand .logo svg{width:20px;height:20px;display:block}
.brand h1{font-size:17px;letter-spacing:.5px;white-space:nowrap}
.brand small{display:block;font-size:10.5px;color:var(--dim);font-weight:400;letter-spacing:2px}
#search{flex:1;min-width:0;max-width:460px;margin-left:auto;
  background:var(--panel);border:1px solid var(--line);border-radius:999px;
  color:var(--text);padding:9px 14px 9px 38px;font-size:14.5px;outline:none;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='16' height='16' fill='none' stroke='%238a93a8' stroke-width='2' stroke-linecap='round'%3E%3Ccircle cx='7' cy='7' r='5'/%3E%3Cpath d='m11.5 11.5 3.5 3.5'/%3E%3C/svg%3E");
  background-repeat:no-repeat;background-position:13px center}
#search:focus{border-color:var(--acc);box-shadow:0 0 0 3px rgba(109,155,255,.15)}
main{position:relative;z-index:1;max-width:1200px;margin:0 auto;padding:20px 18px 60px}
.sec{display:flex;align-items:baseline;gap:10px;margin:22px 0 12px}
.sec.wide{grid-column:1/-1;margin:8px 0 4px}
.sec.wide:first-child{margin-top:0}
.sec h2{font-size:15.5px;font-weight:600}
.sec span{font-size:12.5px;color:var(--dim)}
#crumb{display:flex;flex-wrap:wrap;gap:6px;align-items:center;font-size:13px;color:var(--dim);margin-bottom:4px}
#crumb a{color:var(--dim);text-decoration:none;background:var(--panel);border:1px solid var(--line);
  border-radius:999px;padding:5px 13px;transition:.15s}
#crumb a:hover{color:var(--text);border-color:var(--line2)}
#crumb b{color:var(--text);font-weight:500;background:var(--panel);border:1px solid var(--line2);
  border-radius:999px;padding:5px 13px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(168px,1fr));gap:16px}
@media(min-width:760px){.grid{grid-template-columns:repeat(auto-fill,minmax(215px,1fr));gap:20px}}
/* 目录卡片 */
.dcard{position:relative;background:var(--card);border:1px solid var(--line);border-radius:16px;
  padding:18px 16px 15px;cursor:pointer;transition:.18s;display:flex;flex-direction:column;gap:10px;min-height:118px}
.dcard:hover{border-color:var(--line2);transform:translateY(-2px);box-shadow:var(--shadow)}
.dicon{width:40px;height:40px;border-radius:11px;background:var(--dicon-bg);
  border:1px solid var(--dicon-border);display:flex;align-items:center;justify-content:center;font-size:20px}
.dname{font-size:15.5px;font-weight:600}
.dpath{font-size:11.5px;color:var(--dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.dcount{margin-top:auto;font-size:12px;color:var(--acc)}
.dcard.add{border-style:dashed;border-color:var(--addchip-border);align-items:center;justify-content:center;
  color:var(--acc);font-size:14px;gap:8px;background:transparent}
.dcard.add .plus{font-size:26px;font-weight:300;line-height:1}
.dcard .dx{position:absolute;top:9px;right:9px;width:26px;height:26px;border-radius:8px;
  background:var(--dx-bg);border:1px solid var(--line);color:var(--dim);
  font-size:11px;cursor:pointer;display:flex;align-items:center;justify-content:center}
.dcard .dx:hover{color:#ff7b7b;border-color:#5b3238}
/* 文件夹胶囊行 */
.chips{display:flex;gap:10px;overflow-x:auto;padding:2px 2px 8px;scrollbar-width:thin}
.chip{flex:0 0 auto;display:inline-flex;align-items:center;gap:8px;background:var(--card);
  border:1px solid var(--line);border-radius:13px;color:var(--text);padding:10px 15px;
  font-size:14px;text-decoration:none;cursor:pointer;transition:.15s}
.chip:hover{border-color:var(--line2)}
.chip .n{font-size:11.5px;color:var(--dim)}
/* 视频卡片 */
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;overflow:hidden;
  cursor:pointer;position:relative;transition:.18s}
@media(hover:hover){.card:hover{transform:translateY(-2px);border-color:var(--line2);
  box-shadow:var(--shadow)}}
.card:active{transform:scale(.98)}
.thumb{position:relative;aspect-ratio:16/9;background:var(--thumb-bg);display:flex;
  align-items:center;justify-content:center;color:var(--thumb-icon);font-size:30px;overflow:hidden}
.thumb canvas{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.dur{position:absolute;right:8px;bottom:8px;z-index:3;background:var(--dur-bg);
  backdrop-filter:blur(4px);-webkit-backdrop-filter:blur(4px);color:var(--dur-text);font-size:11px;
  padding:3px 7px;border-radius:7px;display:none}
.dur.show{display:block}
.pbar{position:absolute;left:0;bottom:0;height:3px;background:var(--grad);width:0;z-index:2}
.cardinfo{padding:11px 13px 13px}
.title{font-size:13.5px;font-weight:500;line-height:1.45;display:-webkit-box;-webkit-line-clamp:2;
  -webkit-box-orient:vertical;overflow:hidden;word-break:break-all;min-height:2.9em}
.meta{margin-top:7px;font-size:11.5px;color:var(--dim);display:flex;gap:9px;flex-wrap:wrap}
.meta .seen{color:var(--acc)}
.empty{color:var(--dim);text-align:center;padding:70px 20px;line-height:2.1;font-size:14.5px}
.empty code{background:var(--panel);padding:2px 9px;border-radius:6px;font-size:13px;border:1px solid var(--line)}
.loading{color:var(--dim);text-align:center;padding:60px 0;font-size:14px;animation:pulse 1.2s infinite}
@keyframes pulse{50%{opacity:.35}}
/* 播放页 */
#player{position:fixed;left:0;right:0;bottom:0;top:var(--header-h,0px);background:var(--player-bg);
  z-index:50;display:none;flex-direction:column;
  opacity:0;transform:translateY(14px);transition:opacity .22s,transform .22s}
#player.on{display:flex;opacity:1;transform:none}
#pscroll{flex:1;min-height:0;overflow-y:auto;-webkit-overflow-scrolling:touch}
.vwrap{position:relative;background:#000;overflow:hidden;
  transition:height .36s cubic-bezier(.33,0,.18,1), opacity .36s ease}
#player.playing .vwrap{position:sticky;top:0;z-index:5}   /* 播放中吸顶；窄屏暂停后下滑可平滑收起 */
#vblur{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;display:none;
  filter:blur(26px) saturate(1.15) brightness(.72);transform:scale(1.12)}
.vwrap video{position:relative;z-index:1;width:100%;max-height:56vh;display:block;
  object-fit:contain;outline:none}
#back{position:absolute;top:calc(12px + env(safe-area-inset-top));left:14px;z-index:6;
  width:40px;height:40px;border-radius:50%;border:1px solid var(--back-border);
  background:var(--back-bg);color:var(--back-text);font-size:17px;cursor:pointer;
  backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px)}
#back:hover{background:rgba(10,12,16,.75)}
.pbody{max-width:900px;margin:0 auto;padding:18px 18px 44px;width:100%}
.pfolder{font-size:12.5px;color:var(--dim);margin-bottom:8px}
.pfolder a{color:var(--acc);text-decoration:none}
.ptitle{font-size:17px;font-weight:650;line-height:1.55;word-break:break-all;cursor:pointer;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.ptitle.open{-webkit-line-clamp:unset;display:block}
.chips2{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0}
.tag{font-size:12px;color:var(--dim);background:var(--panel);border:1px solid var(--line);
  border-radius:999px;padding:5px 12px}
.tag.hl{color:var(--hl-text);border-color:var(--hl-border);background:var(--hl-bg)}
#restart{background:var(--grad);border:none;color:#fff;border-radius:11px;
  padding:11px 22px;font-size:14px;font-weight:500;cursor:pointer;
  box-shadow:0 4px 18px rgba(91,140,255,.3)}
#restart:active{transform:scale(.97)}
#renameBtn,#favBtn{background:var(--panel);border:1px solid var(--line2);color:var(--text);
  border-radius:11px;padding:11px 20px;font-size:14px;cursor:pointer}
#renameBtn:active,#favBtn:active{transform:scale(.97)}
.pact{display:flex;gap:10px;margin:4px 0 6px;flex-wrap:wrap}
.moreh{margin:26px 0 12px;font-size:14px;font-weight:600;color:var(--text)}
.moreh span{color:var(--dim);font-weight:400;font-size:12.5px}
#more{margin-top:0;grid-template-columns:repeat(2,1fr)}
@media(min-width:760px){#more{grid-template-columns:repeat(auto-fill,minmax(215px,1fr))}}
/* ---------------- 宽屏：双栏播放布局（视频吸左，信息在右） ---------------- */
@media(min-width:1000px){
  #pscroll{display:grid;grid-template-columns:minmax(0,1.45fr) minmax(340px,1fr);
    column-gap:22px;align-items:start;max-width:1600px;margin:0 auto;padding:0 22px}
  .vwrap{grid-column:1;align-self:start;position:sticky;top:0;border-radius:0 0 14px 14px}
  .pbody{grid-column:2;grid-row:1;padding:18px 0 44px;max-width:none}
  .vwrap video{max-height:calc(100vh - var(--header-h,60px) - 20px)}
}
/* 对话框 / 提示 */
#mask{position:fixed;inset:0;background:var(--mask);backdrop-filter:blur(3px);z-index:80;
  display:none;align-items:center;justify-content:center;padding:24px}
#mask.on,#favmask.on{display:flex}
#favmask{position:fixed;inset:0;background:var(--mask);backdrop-filter:blur(3px);z-index:80;
  display:none;align-items:center;justify-content:center;padding:24px}
.dlg{background:var(--panel);border:1px solid var(--line2);border-radius:16px;
  padding:20px 18px 16px;width:100%;max-width:430px;box-shadow:var(--shadow-lg)}
#favlist{max-height:260px;overflow-y:auto;margin-bottom:12px}
.favrow{display:flex;align-items:center;gap:10px;padding:10px 2px;font-size:14px;
  border-bottom:1px solid var(--line);cursor:pointer}
.favrow:last-child{border-bottom:none}
.favrow input[type=checkbox]{width:17px;height:17px;accent-color:var(--acc);flex:0 0 auto}
.favrow .favcount{margin-left:auto;color:var(--dim);font-size:12px}
.favnew{display:flex;gap:8px;margin-bottom:14px}
.favnew input{flex:1;min-width:0;background:var(--bg);border:1px solid var(--line);border-radius:10px;
  color:var(--text);padding:10px 12px;font-size:13.5px;outline:none}
.favnew input:focus{border-color:var(--acc)}
.favnew button{background:var(--acc);border:none;color:#fff;border-radius:10px;
  padding:10px 16px;font-size:13.5px;cursor:pointer;flex:0 0 auto}
#favBtn.faved{color:var(--hl-text);border-color:var(--hl-border);background:var(--hl-bg)}
.dlgt{font-size:15px;font-weight:550;margin-bottom:14px;line-height:1.6;word-break:break-all}
#dlgi{width:100%;background:var(--bg);border:1px solid var(--line);border-radius:10px;
  color:var(--text);padding:11px 13px;font-size:14px;outline:none;margin-bottom:16px}
#dlgi:focus{border-color:var(--acc);box-shadow:0 0 0 3px rgba(109,155,255,.15)}
.dlgb{display:flex;gap:10px;justify-content:flex-end}
.dlgb button{border-radius:10px;padding:10px 20px;font-size:14px;cursor:pointer;border:1px solid var(--line)}
#dlgn{background:none;color:var(--dim)}
#dlgy{background:var(--grad);border:none!important;color:#fff;font-weight:500}
#toast{position:fixed;left:50%;bottom:34px;transform:translateX(-50%);background:var(--panel);
  border:1px solid var(--line2);color:var(--text);padding:11px 20px;border-radius:12px;font-size:13.5px;
  z-index:99;opacity:0;transition:opacity .3s;pointer-events:none;box-shadow:var(--shadow)}
#toast.show{opacity:1}
#resumePill{position:fixed;right:16px;bottom:calc(22px + env(safe-area-inset-bottom));z-index:60;
  background:var(--grad);border:none;color:#fff;border-radius:999px;padding:12px 22px;
  font-size:14px;font-weight:500;cursor:pointer;display:none;
  box-shadow:0 8px 28px rgba(91,140,255,.45)}
#resumePill.show{display:block}
#resumePill:active{transform:scale(.96)}
#themeBtn{flex:0 0 auto;width:38px;height:38px;border-radius:50%;background:var(--panel);
  border:1px solid var(--line);color:var(--text);font-size:16px;cursor:pointer;
  display:flex;align-items:center;justify-content:center}
#themeBtn:active{transform:scale(.92)}
::-webkit-scrollbar{width:9px;height:9px}
::-webkit-scrollbar-thumb{background:var(--scroll-thumb);border-radius:99px;border:2px solid var(--bg)}
::-webkit-scrollbar-track{background:transparent}
</style>
</head>
<body>

<header>
  <div class="hrow">
    <div class="brand" id="brandHome" title="回到首页">
      <div class="logo"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="#fff" d="M18 4l2 4h-3l-2-4h-2l2 4h-3l-2-4H8l2 4H7L5 4H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V4h-4z"/></svg></div>
      <div><h1>局域网视频站<small>LAN VIDEO</small></h1></div>
    </div>
    <input id="search" type="search" placeholder="搜索视频…" autocomplete="off">
    <button id="themeBtn" title="切换白天/黑夜模式">☀️</button>
  </div>
</header>

<main>
  <div id="crumb"></div>
  <div id="browse">
    <div id="fold"></div>
    <div class="grid" id="grid"></div>
    <div id="emptybox"></div>
  </div>
  <div id="results" style="display:none">
    <div class="sec"><h2>搜索结果</h2><span id="rcount"></span></div>
    <div class="grid" id="rgrid"></div>
    <div id="rempty" class="empty" style="display:none">没有匹配的视频</div>
  </div>
</main>

<div id="player">
  <div id="pscroll">
    <div class="vwrap">
      <canvas id="vblur" width="160" height="90"></canvas>
      <button id="back" title="返回">←</button>
      <video id="v" controls playsinline webkit-playsinline preload="auto"></video>
    </div>
    <div class="pbody">
      <div class="pfolder" id="pfolder"></div>
      <div class="ptitle" id="ptitle"></div>
      <div class="chips2" id="pchips"></div>
      <div class="pact">
        <button id="restart">↻&nbsp; 从头播放</button>
        <button id="renameBtn">✏️&nbsp; 重命名</button>
        <button id="favBtn">☆&nbsp; 收藏</button>
      </div>
      <div class="moreh" id="moreh" style="display:none"></div>
      <div class="grid" id="more" style="display:none"></div>
    </div>
  </div>
  <button id="resumePill">▶&nbsp; 继续播放</button>
</div>

<div id="toast"></div>

<div id="mask">
  <div class="dlg">
    <div class="dlgt" id="dlgt"></div>
    <input id="dlgi" autocomplete="off" spellcheck="false">
    <div class="dlgb">
      <button id="dlgn">取消</button>
      <button id="dlgy">确定</button>
    </div>
  </div>
</div>

<div id="favmask">
  <div class="dlg">
    <div class="dlgt">收藏到</div>
    <div id="favlist"></div>
    <div class="favnew">
      <input id="favnewname" placeholder="新建收藏夹…" maxlength="30">
      <button id="favnewbtn">创建</button>
    </div>
    <div class="dlgb"><button id="favdone">完成</button></div>
  </div>
</div>

<script>
"use strict";
const $ = s => document.querySelector(s);
const LS_KEY = "lan_video_progress";
let curDir = "";
let searchTimer = null;
const itemsCache = {};
let curItem = null, lastSaveAt = 0;

/* ---------------- 工具 ---------------- */
const ICON_CLAP = '<svg viewBox="0 0 24 24" style="width:14px;height:14px;vertical-align:-2px" fill="currentColor" aria-hidden="true"><path d="M18 4l2 4h-3l-2-4h-2l2 4h-3l-2-4H8l2 4H7L5 4H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V4h-4z"/></svg>';

function fmtSize(n){
  if(n >= 1073741824) return (n/1073741824).toFixed(2)+" GB";
  if(n >= 1048576)    return (n/1048576).toFixed(1)+" MB";
  return Math.max(1, Math.round(n/1024))+" KB";
}
function fmtTime(t){
  if(!isFinite(t)) return "";
  t = Math.round(t);
  const h = Math.floor(t/3600), m = Math.floor(t%3600/60), s = t%60;
  const mm = (h? String(m).padStart(2,"0") : m), ss = String(s).padStart(2,"0");
  return h ? h+":"+mm+":"+ss : m+":"+ss;
}
function fmtDate(ts){
  const d = new Date(ts*1000), p = n => String(n).padStart(2,"0");
  return d.getFullYear()+"-"+p(d.getMonth()+1)+"-"+p(d.getDate());
}
function getProgress(){ try{ return JSON.parse(localStorage.getItem(LS_KEY)||"{}"); }catch(e){ return {}; } }
function setProgress(p){ try{ localStorage.setItem(LS_KEY, JSON.stringify(p)); }catch(e){} }
function toast(msg){
  const el = $("#toast"); el.textContent = msg; el.classList.add("show");
  clearTimeout(toast._t); toast._t = setTimeout(()=>el.classList.remove("show"), 2200);
}

/* ---------------- 缩略图（按原始比例居中裁剪 + IndexedDB 持久缓存） ----------------
   缓存键 = 文件大小_修改时间：刷新页面直接取缓存秒出；改名/移动后内容未变仍命中；
   文件被替换（大小或时间变化）则自动重新生成 */
const tdb = {
  db: null, _p: null,
  open(){
    if(this._p) return this._p;
    this._p = new Promise(res=>{
      try{
        const rq = indexedDB.open("lan_video_thumbs", 1);
        rq.onupgradeneeded = ()=> rq.result.createObjectStore("thumbs");
        rq.onsuccess = ()=>{ this.db = rq.result; res(this.db); };
        rq.onerror = ()=> res(null);
      }catch(e){ res(null); }
    });
    return this._p;
  },
  async get(k){
    const db = await this.open(); if(!db) return null;
    return new Promise(res=>{
      try{
        const rq = db.transaction("thumbs").objectStore("thumbs").get(k);
        rq.onsuccess = ()=> res(rq.result || null);
        rq.onerror = ()=> res(null);
      }catch(e){ res(null); }
    });
  },
  async put(k, v){
    const db = await this.open(); if(!db) return;
    try{
      const t = db.transaction("thumbs","readwrite").objectStore("thumbs").put(v, k);
      t.onsuccess = ()=>{}; t.onerror = ()=>{};
    }catch(e){}
  }
};

const thumbQueue = []; let thumbBusy = false;
let thumbPaused = false;   // 播放中暂停生成缩略图，把磁盘带宽让给播放
const thumbObserver = new IntersectionObserver(entries=>{
  for(const e of entries){
    if(e.isIntersecting && !e.target.dataset.queued){
      e.target.dataset.queued = "1";
      thumbQueue.push(e.target);
      pumpThumbs();
    }
  }
},{rootMargin:"150px"});

function pumpThumbs(){
  if(thumbPaused || thumbBusy || !thumbQueue.length) return;
  thumbBusy = true;
  const box = thumbQueue.shift();
  makeThumb(box).finally(()=>{ thumbBusy = false; pumpThumbs(); });
}
// 看门狗：任何原因导致的队列停顿都会被自动拉起
setInterval(()=>{ if(!thumbPaused) pumpThumbs(); }, 2500);

function drawThumbFromURL(box, url, dur){
  const canvas = box.querySelector("canvas");
  const img = new Image();
  img.onload = ()=>{
    canvas.width = 320; canvas.height = 180;
    canvas.getContext("2d").drawImage(img, 0, 0, 320, 180);
    canvas.style.display = "block";
  };
  img.onerror = ()=>{};
  img.src = url;
  if(dur) box.dispatchEvent(new CustomEvent("dur", {detail: dur}));
}

function makeThumb(box){
  return new Promise(async resolve=>{
    const path = box.dataset.path, canvas = box.querySelector("canvas");
    const key = box.dataset.key || "";

    // ① 浏览器本地缓存（IndexedDB）：最快
    if(key){
      const c = await tdb.get(key);
      if(c && c.u){
        drawThumbFromURL(box, c.u, c.d);
        resolve(true);
        return;
      }
    }

    // ② 服务器磁盘缓存（.thumbcache）：其他设备生成过就直接用
    try{
      const r = await fetch("/thumb?path=" + encodeURIComponent(path) + "&v=" + (box.dataset.mtime || ""));
      if(r.ok){
        const dur = parseFloat(r.headers.get("X-Dur") || "0");
        const blob = await r.blob();
        const url = URL.createObjectURL(blob);
        await new Promise(res=>{
          const img = new Image();
          img.onload = ()=>{
            canvas.width = 320; canvas.height = 180;
            canvas.getContext("2d").drawImage(img, 0, 0, 320, 180);
            canvas.style.display = "block";
            res();
          };
          img.onerror = ()=> res();
          img.src = url;
        });
        URL.revokeObjectURL(url);
        if(key && canvas.style.display === "block"){
          tdb.put(key, {u: canvas.toDataURL("image/jpeg", 0.72), d: dur});
          if(dur) box.dispatchEvent(new CustomEvent("dur", {detail: dur}));
        }
        resolve(true);
        return;
      }
    }catch(e){ /* 服务器没有缓存，走现场生成 */ }

    // ③ 现场生成，然后写回本地缓存并上传给服务器供其他设备使用
    const v = document.createElement("video");
    v.muted = true; v.preload = "metadata"; v.playsInline = true;
    const done = ok => {
      const dur = ok ? (v.duration || 0) : 0;   // 先取时长（清理后 v.duration 会变 NaN）
      if(ok){
        canvas.style.display = "block";
        try{
          if(key) tdb.put(key, {u: canvas.toDataURL("image/jpeg", 0.72), d: dur});
          canvas.toBlob(b=>{
            if(b) fetch("/thumb?path=" + encodeURIComponent(path) + "&d=" + dur,
                       {method:"POST", body:b}).catch(()=>{});
          }, "image/jpeg", 0.72);
        }catch(e){}
      }
      v.onloadedmetadata = v.onseeked = v.onerror = null;
      v.removeAttribute("src"); v.load();
      resolve(ok);
    };
    const timer = setTimeout(()=>done(false), 45000);   // 慢盘首次打开可能触发安全扫描，需要更久
    v.onerror = ()=>{ clearTimeout(timer); done(false); };
    v.onloadedmetadata = ()=>{
      const d = v.duration;
      if(d) box.dispatchEvent(new CustomEvent("dur", {detail:d}));
      v.currentTime = Math.min(3, Math.max(0.1, d*0.08)) || 0.1;
    };
    v.onseeked = ()=>{
      clearTimeout(timer);
      try{
        const W = 320, H = 180;
        canvas.width = W; canvas.height = H;
        const ctx = canvas.getContext("2d");
        const vw = v.videoWidth, vh = v.videoHeight;
        if(vw && vh){
          const s = Math.max(W/vw, H/vh);   // 等比缩放到铺满，再居中裁剪
          ctx.drawImage(v, (W-vw*s)/2, (H-vh*s)/2, vw*s, vh*s);
        }else{
          ctx.drawImage(v, 0, 0, W, H);
        }
        done(true);
      }catch(e){ done(false); }
    };
    v.src = "/video?path=" + encodeURIComponent(path);
  });
}

function attachThumb(path, cacheKey, mtime){
  const thumb = document.createElement("div");
  thumb.className = "thumb"; thumb.dataset.path = path;
  if(cacheKey) thumb.dataset.key = cacheKey;
  if(mtime) thumb.dataset.mtime = mtime;
  thumb.innerHTML = "🎞️<canvas style='display:none'></canvas>";
  thumbObserver.observe(thumb);
  return thumb;
}

function durBadge(thumb){
  const b = document.createElement("span");
  b.className = "dur";
  thumb.appendChild(b);
  thumb.addEventListener("dur", e=>{
    b.textContent = fmtTime(e.detail);
    b.classList.add("show");
  });
  return b;
}

/* ---------------- 卡片 ---------------- */
function videoCard(item){
  const card = document.createElement("div");
  card.className = "card";
  itemsCache[item.path] = item;
  const prog = getProgress()[item.path];
  const pct = prog && prog.d ? Math.min(100, prog.t/prog.d*100) : 0;

  const thumb = attachThumb(item.path, (item.size && item.mtime) ? item.size + "_" + item.mtime : "", item.mtime);
  durBadge(thumb);
  card.appendChild(thumb);

  if(pct > 1){
    const bar = document.createElement("div");
    bar.className = "pbar"; bar.style.width = pct.toFixed(1)+"%";
    thumb.appendChild(bar);
  }

  const info = document.createElement("div");
  info.className = "cardinfo";
  const t = document.createElement("div"); t.className = "title"; t.title = item.name;
  t.textContent = item.display || item.name;
  const meta = document.createElement("div"); meta.className = "meta";
  meta.append(Object.assign(document.createElement("span"), {textContent: fmtSize(item.size)}),
              Object.assign(document.createElement("span"), {textContent: fmtDate(item.mtime)}));
  if(pct > 1) meta.append(Object.assign(document.createElement("span"),
              {className: "seen", textContent: "看过 " + Math.round(pct) + "%"}));
  info.append(t, meta);
  card.appendChild(info);
  card.addEventListener("click", ()=>{
    location.hash = "#/p/" + encodeURIComponent(item.path);
  });
  return card;
}

function dirCard(f){
  const card = document.createElement("div");
  card.className = "dcard";
  const icon = document.createElement("div"); icon.className = "dicon"; icon.textContent = "📁";
  const name = document.createElement("div"); name.className = "dname"; name.textContent = f.name;
  card.append(icon, name);
  if(f.full){   // 顶层目录显示磁盘路径
    const path = document.createElement("div"); path.className = "dpath";
    path.textContent = f.full; path.title = f.full;
    card.appendChild(path);
  }
  const count = document.createElement("div"); count.className = "dcount";
  count.textContent = (f.count || 0) + " 个视频";
  card.appendChild(count);
  if(f.removable){
    const x = document.createElement("button");
    x.className = "dx"; x.textContent = "✕"; x.title = "移除该目录";
    x.addEventListener("click", e=>{ e.stopPropagation(); removeDir(f.name); });
    card.appendChild(x);
  }
  card.addEventListener("click", ()=>{
    location.hash = "#/d/" + encodeURIComponent(f.path);
  });
  return card;
}

function addDirCard(){
  const card = document.createElement("div");
  card.className = "dcard add";
  card.innerHTML = "<div class='plus'>＋</div><div>添加视频目录</div>";
  card.addEventListener("click", addDir);
  return card;
}

function folderChip(f){
  const a = document.createElement("a");
  a.className = "chip"; a.href = "#/d/"+encodeURIComponent(f.path);
  a.textContent = "📁 " + f.name;
  if(f.count !== undefined){
    const n = document.createElement("span"); n.className = "n"; n.textContent = f.count;
    a.appendChild(n);
  }
  return a;
}

/* ---------------- 应用内对话框 ---------------- */
let dlgOk = null;
function askDialog({title, input = null, yes = "确定", onOk}){
  $("#dlgt").textContent = title;
  const i = $("#dlgi");
  if(input){
    i.style.display = ""; i.value = input.value || ""; i.placeholder = input.placeholder || "";
  }else{
    i.style.display = "none"; i.value = "";
  }
  $("#dlgy").textContent = yes;
  dlgOk = onOk;
  $("#mask").classList.add("on");
  if(input) setTimeout(()=>i.focus(), 60);
}
function closeDialog(){ $("#mask").classList.remove("on"); dlgOk = null; }
$("#dlgy").addEventListener("click", ()=>{
  const f = dlgOk; closeDialog();
  if(f) f($("#dlgi").value.trim());
});
$("#dlgn").addEventListener("click", closeDialog);
$("#mask").addEventListener("click", e=>{ if(e.target.id === "mask") closeDialog(); });
$("#dlgi").addEventListener("keydown", e=>{ if(e.key === "Enter") $("#dlgy").click(); });

/* ---------------- 目录管理 ---------------- */
function addDir(){
  askDialog({
    title: "添加视频目录",
    input: {value: "E:\\", placeholder: "文件夹完整路径，例如 E:\\新建文件夹\\3fui"},
    yes: "添加",
    onOk: async p => {
      if(!p) return;
      const r = await fetch("/api/dirs", {method:"POST",
        headers:{"Content-Type":"application/json"},
        body: JSON.stringify({action:"add", path:p})});
      const d = await r.json();
      if(d.ok){ toast("已添加目录：" + d.entry.alias); openDir(""); }
      else toast(d.error || "添加失败");
    },
  });
}

function removeDir(alias){
  askDialog({
    title: `移除目录「${alias}」？只是不再播放，不会删除里面的文件。`,
    yes: "移除",
    onOk: async () => {
      const r = await fetch("/api/dirs", {method:"POST",
        headers:{"Content-Type":"application/json"},
        body: JSON.stringify({action:"remove", alias})});
      const d = await r.json();
      if(d.ok){ toast("已移除目录"); openDir(""); }
      else toast(d.error || "移除失败");
    },
  });
}

/* ---------------- 浏览 / 搜索 ---------------- */
function renderCrumb(dir){
  const crumb = $("#crumb"); crumb.innerHTML = "";
  const root = document.createElement("a");
  root.href = "#/d/"; root.innerHTML = ICON_CLAP + '<span style="margin-left:5px">全部</span>'; crumb.appendChild(root);
  const segs = dir.split("/").filter(Boolean);
  let acc = "";
  segs.forEach((seg, i)=>{
    acc += (acc ? "/" : "") + seg;
    crumb.append(" ");
    if(i === segs.length - 1){
      const here = document.createElement("b");
      here.textContent = seg;
      crumb.appendChild(here);
    }else{
      const a = document.createElement("a");
      a.href = "#/d/"+encodeURIComponent(acc); a.textContent = seg;
      crumb.appendChild(a);
    }
  });
}

function wideSection(title, sub){
  const s = document.createElement("div");
  s.className = "sec wide";
  s.innerHTML = `<h2>${title}</h2><span>${sub}</span>`;
  return s;
}

async function renderBrowse(data){
  $("#results").style.display = "none";
  $("#browse").style.display = "";
  renderCrumb(data.cur);
  const isRoot = !data.cur;

  const grid = $("#grid"); grid.innerHTML = "";
  const box = $("#emptybox"); box.innerHTML = "";
  grid.className = "grid";
  grid.style.gridTemplateColumns = isRoot ? "repeat(auto-fill,minmax(240px,1fr))" : "";

  if(isRoot){   // 首页顶部：收藏夹区
    try{
      const fr = await fetch("/api/favs");
      const fd = await fr.json();
      if(fd.folders && fd.folders.length){
        grid.appendChild(wideSection("⭐ 收藏夹", fd.folders.length + " 个"));
        fd.folders.forEach(f=>grid.appendChild(favFolderCard(f)));
      }
    }catch(e){}
  }

  if(data.folders.length){                       // 子文件夹：宫格卡片
    grid.appendChild(wideSection("文件夹", data.folders.length + " 个"));
    data.folders.forEach(f=>grid.appendChild(dirCard(f)));
    if(isRoot) grid.appendChild(addDirCard());
  }
  if(data.videos.length){
    grid.appendChild(wideSection("视频", data.videos.length + " 个"));
    data.videos.forEach(v=>grid.appendChild(videoCard(v)));
  }
  if(!data.folders.length && !data.videos.length){
    box.className = "empty";
    box.innerHTML = isRoot
      ? "还没有视频目录<br>点上方 <b>＋ 添加视频目录</b>，或把视频放进服务目录下的「视频」文件夹"
      : "这个文件夹里没有视频";
  }
  pumpThumbs();
}

function showSearch(list, q){
  $("#browse").style.display = "none";
  $("#results").style.display = "";
  $("#rcount").textContent = `“${q}” · ${list.length} 个`;
  const grid = $("#rgrid"); grid.innerHTML = "";
  $("#rempty").style.display = list.length ? "none" : "";
  list.forEach(v=>{
    const item = Object.assign({}, v, {
      name: v.name.split("/").pop(),
      display: v.folder ? v.folder + " › " + v.name.split("/").pop() : v.name,
    });
    grid.appendChild(videoCard(item));
  });
  pumpThumbs();
}

let navSeq = 0;   // 渲染序号：慢的旧响应不覆盖新的（搜索/切换目录竞态保护）

async function openDir(dir){
  const seq = ++navSeq;
  $("#emptybox").innerHTML = "";
  $("#emptybox").className = "loading";
  $("#emptybox").textContent = "加载中…";
  const r = await fetch("/api/list?dir="+encodeURIComponent(dir||""));
  if(!r.ok){ toast("打开目录失败"); return; }
  if(seq !== navSeq) return;   // 期间用户又导航了，丢弃这次结果
  await renderBrowse(await r.json());
}

/* ---------------- 收藏夹页面 ---------------- */
function favFolderCard(f){
  const card = document.createElement("div");
  card.className = "dcard";
  const icon = document.createElement("div"); icon.className = "dicon"; icon.textContent = "⭐";
  const name = document.createElement("div"); name.className = "dname"; name.textContent = f.name;
  const count = document.createElement("div"); count.className = "dcount";
  count.textContent = (f.count || 0) + " 个视频";
  card.append(icon, name, count);
  card.addEventListener("click", ()=>{
    location.hash = "#/fav/" + encodeURIComponent(f.id);
  });
  return card;
}

function addFavCard(){
  const card = document.createElement("div");
  card.className = "dcard add";
  card.innerHTML = "<div class='plus'>＋</div><div>新建收藏夹</div>";
  card.addEventListener("click", newFavFolderDialog);
  return card;
}

async function openFavHome(){
  $("#results").style.display = "none";
  $("#browse").style.display = "";
  const crumb = $("#crumb"); crumb.innerHTML = "";
  const root = document.createElement("a");
  root.href = "#/d/"; root.innerHTML = ICON_CLAP + '<span style="margin-left:5px">全部</span>'; crumb.appendChild(root);
  crumb.append(" ");
  const here = document.createElement("b"); here.textContent = "⭐ 收藏夹";
  crumb.appendChild(here);

  const r = await fetch("/api/favs");
  const d = await r.json();
  const grid = $("#grid"); grid.innerHTML = "";
  const box = $("#emptybox"); box.innerHTML = "";
  grid.className = "grid";
  grid.style.gridTemplateColumns = "repeat(auto-fill,minmax(240px,1fr))";
  grid.appendChild(wideSection("⭐ 收藏夹", d.folders.length + " 个"));
  d.folders.forEach(f=>grid.appendChild(favFolderCard(f)));
  grid.appendChild(addFavCard());
}

async function openFavItems(fid){
  const r = await fetch("/api/favs?fid=" + encodeURIComponent(fid));
  const d = await r.json();
  if(!d.ok){ toast("收藏夹不存在"); location.hash = "#/fav"; return; }
  $("#results").style.display = "none";
  $("#browse").style.display = "";
  const crumb = $("#crumb"); crumb.innerHTML = "";
  const root = document.createElement("a");
  root.href = "#/d/"; root.innerHTML = ICON_CLAP + '<span style="margin-left:5px">全部</span>'; crumb.appendChild(root);
  crumb.append(" ");
  const favLink = document.createElement("a");
  favLink.href = "#/fav"; favLink.textContent = "⭐ 收藏夹";
  crumb.appendChild(favLink);
  crumb.append(" ");
  const here = document.createElement("b"); here.textContent = d.folder.name;
  crumb.appendChild(here);

  const grid = $("#grid"); grid.innerHTML = "";
  const box = $("#emptybox"); box.innerHTML = "";
  grid.className = "grid";
  grid.style.gridTemplateColumns = "";
  grid.appendChild(wideSection("⭐ " + d.folder.name, d.items.length + " 个"));

  if(!d.folder.default){   // 自建收藏夹的管理按钮
    const row = document.createElement("div");
    row.className = "pact"; row.style.gridColumn = "1/-1"; row.style.margin = "0 0 10px";
    const rn = document.createElement("button");
    rn.className = "tag"; rn.style.cursor = "pointer"; rn.textContent = "✏️ 重命名收藏夹";
    rn.addEventListener("click", ()=>{
      askDialog({
        title: "重命名收藏夹",
        input: {value: d.folder.name, placeholder: "新的名称"},
        yes: "重命名",
        onOk: async nn => {
          if(!nn || nn === d.folder.name) return;
          const rr = await fetch("/api/favs", {method:"POST",
            headers:{"Content-Type":"application/json"},
            body: JSON.stringify({action:"renamefolder", id:fid, name:nn})});
          const dd = await rr.json();
          if(dd.ok){ toast("已重命名"); openFavItems(fid); } else toast("重命名失败");
        },
      });
    });
    const del = document.createElement("button");
    del.className = "tag"; del.style.cursor = "pointer"; del.textContent = "🗑 删除收藏夹";
    del.addEventListener("click", ()=>{
      askDialog({
        title: `删除收藏夹「${d.folder.name}」？里面的收藏也会移除（视频文件不受影响）。`,
        yes: "删除",
        onOk: async () => {
          const rr = await fetch("/api/favs", {method:"POST",
            headers:{"Content-Type":"application/json"},
            body: JSON.stringify({action:"delfolder", id:fid})});
          const dd = await rr.json();
          if(dd.ok){ toast("已删除收藏夹"); location.hash = "#/fav"; }
          else toast(dd.error || "删除失败");
        },
      });
    });
    row.append(rn, del);
    grid.appendChild(row);
  }

  d.items.forEach(i=>{
    grid.appendChild(videoCard({path: i.path, name: i.name, size: i.size, mtime: i.mtime}));
  });
  pumpThumbs();
  if(!d.items.length){
    box.className = "empty";
    box.innerHTML = "还没有收藏视频<br>打开一个视频，点 <b>⭐ 收藏</b> 添加到这里";
  }
}

async function newFavFolderDialog(){
  askDialog({
    title: "新建收藏夹",
    input: {value: "", placeholder: "收藏夹名称"},
    yes: "创建",
    onOk: async name => {
      if(!name) return;
      const r = await fetch("/api/favs", {method:"POST",
        headers:{"Content-Type":"application/json"},
        body: JSON.stringify({action:"newfolder", name})});
      const d = await r.json();
      if(d.ok){ toast("已创建：" + d.entry.name); openFavHome(); }
      else toast(d.error || "创建失败");
    },
  });
}

async function doSearch(q){
  const seq = ++navSeq;
  const r = await fetch("/api/search?q="+encodeURIComponent(q));
  if(!r.ok || seq !== navSeq) return;
  showSearch(await r.json(), q);
}

/* ---------------- 播放页 ---------------- */
const vblur = $("#vblur");
let lastBlur = 0;
function drawVblur(force){
  if(!curItem) return;
  const v = $("#v");
  if(!v.videoWidth){ vblur.style.display = "none"; return; }
  const now = Date.now();
  if(!force && now - lastBlur < 600) return;   // 节流：约 0.6 秒刷一帧背景
  lastBlur = now;
  try{
    vblur.getContext("2d").drawImage(v, 0, 0, vblur.width, vblur.height);
    vblur.style.display = "block";
  }catch(e){}
}

function saveCurProgress(){
  const v = $("#v");
  if(curItem && v.duration && v.currentTime > 5 && v.currentTime < v.duration*0.95){
    const p = getProgress();
    p[curItem.path] = {t: v.currentTime, d: v.duration};
    setProgress(p);
  }
}

function openPlayerByPath(path){
  saveCurProgress();
  const item = itemsCache[path] || {path, name: path.split("/").pop(), size: 0, mtime: 0};
  curItem = item;
  const v = $("#v");
  lastSaveAt = 0;
  const folder = item.folder !== undefined ? item.folder
                 : path.split("/").slice(0, -1).join("/");

  $("#pfolder").innerHTML = "📁 ";
  const fl = document.createElement("a");
  fl.href = "#/d/" + encodeURIComponent(folder);
  fl.textContent = folder || "全部";
  $("#pfolder").appendChild(fl);
  $("#ptitle").textContent = item.name;
  $("#ptitle").classList.remove("open");   // 新视频默认折叠为两行
  const chips = $("#pchips"); chips.innerHTML = "";
  const addTag = (txt, hl)=>{
    const s = document.createElement("span");
    s.className = "tag" + (hl ? " hl" : "");
    s.textContent = txt; chips.appendChild(s);
  };
  if(item.size) addTag(fmtSize(item.size));
  if(item.mtime) addTag(fmtDate(item.mtime));
  const ext = (item.name.split(".").pop()||"").toUpperCase();
  if(ext) addTag(ext);
  const p0 = getProgress()[path];
  if(p0 && p0.d) addTag("看到 " + Math.round(Math.min(100, p0.t/p0.d*100)) + "%", true);
  $("#restart").style.display = (p0 && p0.t > 5) ? "" : "none";
  $("#player").classList.add("on");
  document.body.classList.add("viewing");
  document.body.style.overflow = "hidden";
  setVwrapCollapsed(false);
  $("#pscroll").scrollTop = 0;

  v.onloadedmetadata = ()=>{
    const p = getProgress()[path];
    if(p && p.t > 5 && (!v.duration || p.t < v.duration*0.95)){
      v.currentTime = p.t;
      toast("已从 " + fmtTime(p.t) + " 继续播放");
    }
    drawVblur(true);
    v.play().catch(()=>{});   // 手机浏览器可能要求手动再点一下播放
  };
  const saveNow = ()=>{
    if(!curItem || !v.duration || v.ended) return;
    lastSaveAt = Date.now();
    const p = getProgress();
    p[curItem.path] = {t: v.currentTime, d: v.duration};
    setProgress(p);
  };
  v.ontimeupdate = ()=>{
    // 播放中每 5 秒保存一次（timeupdate 约 250ms 一次，不能每次都写）
    if(v.duration && curItem && Date.now() - lastSaveAt >= 5000) saveNow();
    drawVblur();
  };
  v.onseeked = ()=> drawVblur(true);
  v.onplay = ()=>{
    thumbPaused = true;          // 播放优先：缩略图生成让出磁盘带宽
    setVwrapCollapsed(false);
    $("#player").classList.add("playing");
    updatePausedView();
  };
  v.onpause = ()=>{
    saveNow();
    drawVblur(true);
    thumbPaused = false;
    pumpThumbs();
    $("#player").classList.remove("playing");   // 暂停后取消吸顶，下滑可收起视频区
    updatePausedView();
  };
  v.onended = ()=>{
    if(!curItem) return;
    thumbPaused = false;
    pumpThumbs();
    const p = getProgress(); delete p[curItem.path]; setProgress(p);
  };
  v.src = "/video?path=" + encodeURIComponent(path);
  loadMore(path, folder);
  refreshFavState();
}

async function loadMore(path, folder){
  const more = $("#more"), moreh = $("#moreh");
  more.innerHTML = ""; more.style.display = "none"; moreh.style.display = "none";
  try{
    const r = await fetch("/api/list?dir=" + encodeURIComponent(folder||""));
    if(!r.ok) return;
    const d = await r.json();
    const others = d.videos.filter(x => x.path !== path);
    if(!others.length) return;
    moreh.innerHTML = `本文件夹更多 <span>（${others.length}）</span>`;
    moreh.style.display = "";
    more.style.display = "";       // 宫格样式，复用主列表卡片
    others.forEach(x=> more.appendChild(videoCard(x)));
  }catch(e){ /* 忽略，更多列表只是锦上添花 */ }
}

/* ---------------- 暂停浏览：向下滚动时平滑收起视频区，方便逛宫格 ---------------- */
let vwrapCollapsed = false;
function setVwrapCollapsed(c){
  if(vwrapCollapsed === c) return;
  vwrapCollapsed = c;
  const vw = document.querySelector(".vwrap");
  vw.style.overflow = "hidden";
  if(c){
    vw.style.height = vw.scrollHeight + "px";          // 先钉住当前高度
    requestAnimationFrame(()=>requestAnimationFrame(()=>{
      vw.style.height = "0px";                         // 再过渡到 0，内容平滑上移
      vw.style.opacity = "0";
    }));
  }else{
    vw.style.display = "";
    vw.style.height = "0px";
    vw.style.opacity = "0";
    requestAnimationFrame(()=>requestAnimationFrame(()=>{
      vw.style.height = vw.scrollHeight + "px";        // 从 0 平滑展开
      vw.style.opacity = "1";
    }));
  }
}
document.querySelector(".vwrap").addEventListener("transitionend", e=>{
  if(e.propertyName !== "height") return;
  const vw = e.target;
  if(!vwrapCollapsed){ vw.style.height = ""; vw.style.opacity = ""; }   // 展开完恢复自适应高度
});
function updatePausedView(){
  const pill = $("#resumePill");
  if(!curItem){ pill.classList.remove("show"); return; }
  const v = $("#v");
  if(v.paused && !v.ended){
    // 窄屏暂停后下滑收起视频区；宽屏是双栏布局，视频始终钉在左栏
    if(window.innerWidth < 1000 && !vwrapCollapsed && $("#pscroll").scrollTop > 140) setVwrapCollapsed(true);
    pill.classList.toggle("show", vwrapCollapsed);
  }else{
    setVwrapCollapsed(false);
    pill.classList.remove("show");
  }
}
$("#pscroll").addEventListener("scroll", updatePausedView, {passive: true});
$("#resumePill").addEventListener("click", ()=>{
  $("#v").play().catch(()=>{});
});

/* ---------------- 重命名（磁盘文件同步改名） ---------------- */
$("#renameBtn").addEventListener("click", ()=>{
  if(!curItem) return;
  askDialog({
    title: "重命名视频",
    input: {value: curItem.name, placeholder: "新的文件名"},
    yes: "重命名",
    onOk: async newName => {
      if(!newName || newName === curItem.name) return;
      const r = await fetch("/api/rename", {method:"POST",
        headers:{"Content-Type":"application/json"},
        body: JSON.stringify({path: curItem.path, name: newName})});
      const d = await r.json();
      if(!d.ok){ toast(d.error || "重命名失败"); return; }

      const oldPath = curItem.path;
      const p = getProgress();                       // 观看进度跟随迁移
      if(p[oldPath]){ p[d.new_path] = p[oldPath]; delete p[oldPath]; setProgress(p); }
      delete itemsCache[oldPath];
      curItem = Object.assign({}, curItem, {path: d.new_path, name: d.new_name});
      itemsCache[d.new_path] = curItem;
      $("#ptitle").textContent = d.new_name;

      // 无缝切到新路径，保持当前播放位置
      const v = $("#v");
      const t = v.currentTime, wasPaused = v.paused;
      v.onloadedmetadata = ()=>{
        if(isFinite(t) && t > 0.1 && v.duration) v.currentTime = Math.min(t, v.duration - 0.2);
        if(!wasPaused) v.play().catch(()=>{});
      };
      v.src = "/video?path=" + encodeURIComponent(d.new_path);
      history.replaceState(null, "", "#/p/" + encodeURIComponent(d.new_path));
      toast("已重命名");
      loadMore(d.new_path, d.new_path.split("/").slice(0, -1).join("/"));
    },
  });
});

/* ---------------- 收藏 ---------------- */
let curFavFids = [];

async function refreshFavState(){
  if(!curItem) return;
  try{
    const r = await fetch("/api/favs?path=" + encodeURIComponent(curItem.path));
    const d = await r.json();
    curFavFids = d.fids || [];
  }catch(e){ curFavFids = []; }
  const b = $("#favBtn");
  if(curFavFids.length){ b.textContent = "⭐ 已收藏"; b.classList.add("faved"); }
  else { b.textContent = "☆ 收藏"; b.classList.remove("faved"); }
}

function closeFavPicker(){ $("#favmask").classList.remove("on"); }

async function openFavPicker(){
  if(!curItem) return;
  let d;
  try{
    const r = await fetch("/api/favs?path=" + encodeURIComponent(curItem.path));
    d = await r.json();
  }catch(e){ toast("加载收藏夹失败"); return; }
  const list = $("#favlist"); list.innerHTML = "";
  d.folders.forEach(f=>{
    const row = document.createElement("label"); row.className = "favrow";
    const cb = document.createElement("input"); cb.type = "checkbox"; cb.checked = !!f.has;
    cb.addEventListener("change", async ()=>{
      const act = cb.checked ? "add" : "remove";
      const rr = await fetch("/api/favs", {method:"POST",
        headers:{"Content-Type":"application/json"},
        body: JSON.stringify({action:act, path:curItem.path, fid:f.id})});
      const dd = await rr.json();
      if(dd.ok){
        f.has = cb.checked;
        curFavFids = f.has ? curFavFids.concat(f.id) : curFavFids.filter(x=>x!==f.id);
        const b = $("#favBtn");
        if(curFavFids.length){ b.textContent = "⭐ 已收藏"; b.classList.add("faved"); }
        else { b.textContent = "☆ 收藏"; b.classList.remove("faved"); }
        toast(cb.checked ? "已收藏到「" + f.name + "」" : "已从「" + f.name + "」移除");
      }else{
        cb.checked = !cb.checked;
        toast(dd.error || "操作失败");
      }
    });
    const sp = document.createElement("span");
    sp.textContent = (f.default ? "⭐ " : "📁 ") + f.name;
    const n = document.createElement("span"); n.className = "favcount";
    n.textContent = f.count + " 个";
    row.append(cb, sp, n);
    list.appendChild(row);
  });
  $("#favnewname").value = "";
  $("#favmask").classList.add("on");
}

$("#favBtn").addEventListener("click", openFavPicker);
$("#favdone").addEventListener("click", closeFavPicker);
$("#favmask").addEventListener("click", e=>{ if(e.target.id === "favmask") closeFavPicker(); });
$("#favnewbtn").addEventListener("click", async ()=>{
  const name = $("#favnewname").value.trim();
  if(!name){ toast("先输入收藏夹名称"); return; }
  const r = await fetch("/api/favs", {method:"POST",
    headers:{"Content-Type":"application/json"},
    body: JSON.stringify({action:"newfolder", name})});
  const d = await r.json();
  if(!d.ok){ toast(d.error || "创建失败"); return; }
  $("#favnewname").value = "";
  if(curItem){   // 创建后自动把当前视频收进去并勾选
    await fetch("/api/favs", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({action:"add", path:curItem.path, fid:d.entry.id})}).catch(()=>{});
  }
  openFavPicker();
});
$("#favnewname").addEventListener("keydown", e=>{ if(e.key === "Enter") $("#favnewbtn").click(); });

function stopPlayback(){
  saveCurProgress();
  const v = $("#v");
  v.pause();
  thumbPaused = false;
  pumpThumbs();
  v.onloadedmetadata = v.ontimeupdate = v.onended = null;
  v.onplay = v.onpause = v.onseeked = null;
  v.removeAttribute("src"); v.load();
  vblur.style.display = "none";
  $("#player").classList.remove("on", "playing");
  document.body.classList.remove("viewing");
  setVwrapCollapsed(false);
  $("#resumePill").classList.remove("show");
  document.body.style.overflow = "";
  curItem = null;
}

$("#ptitle").addEventListener("click", ()=>{
  $("#ptitle").classList.toggle("open");   // 点击展开/收起长文件名
});

$("#restart").addEventListener("click", ()=>{
  if(!curItem) return;
  const p = getProgress(); delete p[curItem.path]; setProgress(p);
  const v = $("#v");
  v.currentTime = 0; v.play().catch(()=>{});
  $("#restart").style.display = "none";
  toast("从头开始播放");
});

/* ---------------- 路由 ---------------- */
function refreshRoute(){
  const h = location.hash;
  const pm = h.match(/^#\/p\/(.+)$/);
  if(pm){ openPlayerByPath(decodeURIComponent(pm[1])); return; }
  const wasPlaying = $("#player").classList.contains("on");
  if(wasPlaying) stopPlayback();
  const fm = h.match(/^#\/fav\/([^/]+)$/);
  if(h === "#/fav"){ openFavHome(); return; }
  if(fm){ openFavItems(decodeURIComponent(fm[1])); return; }
  const dm = h.match(/^#\/d\/(.*)$/);
  const dir = dm ? decodeURIComponent(dm[1]) : "";
  curDir = dir;
  openDir(dir);
}

window.addEventListener("hashchange", refreshRoute);

$("#search").addEventListener("input", e=>{
  const q = e.target.value.trim();
  clearTimeout(searchTimer);
  if(!q){ refreshRoute(); return; }
  if($("#player").classList.contains("on")){
    // 播放页直接搜：保存进度退出播放，显示搜索结果
    stopPlayback();
    location.hash = "#/d/" + encodeURIComponent(curDir);
  }
  searchTimer = setTimeout(()=>doSearch(q), 300);
});

$("#back").addEventListener("click", ()=>{
  // 回到该视频所在目录；直接打开播放链接时也能正常退回首页
  const folder = curItem ? (curItem.folder !== undefined ? curItem.folder
                  : curItem.path.split("/").slice(0, -1).join("/")) : curDir;
  location.hash = "#/d/" + encodeURIComponent(folder);
});

/* ---------------- 顶栏：高度变量 + 点击品牌回首页 ---------------- */
function fitHeaderVar(){
  document.documentElement.style.setProperty("--header-h",
    document.querySelector("header").offsetHeight + "px");
}
window.addEventListener("resize", fitHeaderVar);
fitHeaderVar();
$("#brandHome").addEventListener("click", ()=>{ location.hash = "#/d/"; });

/* ---------------- 白天/黑夜模式 ---------------- */
function applyTheme(t){
  document.documentElement.setAttribute("data-theme", t);
  $("#themeBtn").textContent = (t === "light") ? "🌙" : "☀️";   // 显示点击后切换到的模式
  const m = document.querySelector('meta[name="theme-color"]');
  if(m) m.setAttribute("content", t === "light" ? "#f2f5fa" : "#0a0c10");
  try{ localStorage.setItem("lan_video_theme", t); }catch(e){}
}
$("#themeBtn").addEventListener("click", ()=>{
  applyTheme(document.documentElement.getAttribute("data-theme") === "light" ? "dark" : "light");
});
applyTheme(document.documentElement.getAttribute("data-theme") || "dark");

refreshRoute();
</script>
</body>
</html>
"""


# ================================================================ HTTP 服务

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "LANVideo/3.0"

    def log_message(self, fmt, *args):  # 静默普通访问日志，只留错误
        pass

    # ---------- 基础响应 ----------
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _error(self, code: int, msg: str):
        self._send(code, msg.encode("utf-8"), "text/plain; charset=utf-8")

    def _params(self) -> dict:
        q = urllib.parse.urlsplit(self.path).query
        params = {}
        for part in q.split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                params[k] = urllib.parse.unquote(v)
        return params

    def _body_json(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if n <= 0 or n > 65536:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except (OSError, ValueError):
            return {}

    # ---------- 路由 ----------
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        route = urllib.parse.urlsplit(self.path).path
        try:
            if route in ("/", "/index.html"):
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif route == "/api/list":
                data = list_dir(self._params().get("dir", ""))
                self._json(data) if data else self._error(404, "目录不存在")
            elif route == "/api/search":
                self._json(search(self._params().get("q", "")))
            elif route == "/api/favs":
                p = self._params()
                if "fid" in p:
                    info = FAVS.folder_info(p["fid"])
                    if not info:
                        return self._json({"ok": False, "error": "收藏夹不存在"})
                    return self._json({"ok": True, "folder": info,
                                       "items": FAVS.items_of(p["fid"])})
                if "path" in p:
                    rel = p["path"]
                    fids = FAVS.fids_for(rel)
                    folders = FAVS.folders_with_counts()
                    for d in folders:
                        d["has"] = d["id"] in fids
                    return self._json({"ok": True, "folders": folders, "fids": fids})
                FAVS.validate()   # 清掉源文件已删除的收藏
                return self._json({"ok": True, "folders": FAVS.folders_with_counts()})
            elif route == "/dbg-open":
                p = self._params().get("path", "")
                fs = resolve(p)
                t0 = time.perf_counter()
                try:
                    with open(fs, "rb") as f:
                        n = len(f.read(262144))
                    self._json({"ok": True, "ms": round((time.perf_counter()-t0)*1000), "n": n})
                except Exception as e:
                    self._json({"ok": False, "err": f"{type(e).__name__}: {e}",
                                "ms": round((time.perf_counter()-t0)*1000)})
            elif route == "/video":
                self.stream_video()
            elif route == "/thumb":
                self.get_thumb()
            else:
                self._error(404, "Not Found")
        except (BrokenPipeError, ConnectionResetError):
            pass  # 手机端拖动进度条时会主动断开，属正常现象
        except Exception as e:
            try:
                self._error(500, f"服务器内部错误: {e}")
            except Exception:
                pass

    def do_POST(self):
        route = urllib.parse.urlsplit(self.path).path
        try:
            if route == "/api/dirs":
                body = self._body_json()
                action = body.get("action")
                if action == "add":
                    raw = str(body.get("path") or "").strip().strip('"').strip("'")
                    if not raw:
                        return self._json({"ok": False, "error": "路径为空"})
                    p = os.path.abspath(os.path.expanduser(raw))
                    if not os.path.isdir(p):
                        return self._json({"ok": False, "error": "文件夹不存在：" + p})
                    try:
                        entry = LIB.add(p)
                    except ValueError as e:
                        return self._json({"ok": False, "error": str(e)})
                    LIB.save()
                    threading.Thread(target=warm_library, args=([entry], f"新目录 {entry['alias']}"), daemon=True).start()
                    return self._json({"ok": True, "entry": entry, "dirs": LIB.roots()})
                if action == "remove":
                    ok = LIB.remove(str(body.get("alias") or ""))
                    LIB.save()
                    return self._json({"ok": ok, "dirs": LIB.roots()})
                return self._json({"ok": False, "error": "未知操作"})
            if route == "/api/rename":
                body = self._body_json()
                rel = str(body.get("path") or "")
                new_name = str(body.get("name") or "").strip().rstrip(" .")
                fs_path = resolve(rel)
                if fs_path is None or not os.path.isfile(fs_path):
                    return self._json({"ok": False, "error": "文件不存在"})
                if not new_name:
                    return self._json({"ok": False, "error": "名称为空"})
                if any(c in new_name for c in '\\/:*?"<>|'):
                    return self._json({"ok": False, "error": '名称不能包含 \\/:*?"<>| 字符'})
                if new_name.startswith("."):
                    return self._json({"ok": False, "error": "名称不能以 . 开头"})
                _, ext_old = os.path.splitext(fs_path)
                if not os.path.splitext(new_name)[1]:
                    new_name += ext_old          # 没写扩展名时自动补上原扩展名
                target = os.path.join(os.path.dirname(fs_path), new_name)
                if os.path.normcase(target) != os.path.normcase(fs_path) \
                        and os.path.exists(target):
                    return self._json({"ok": False, "error": "同名文件已存在"})
                try:
                    os.rename(fs_path, target)   # 磁盘上的文件同步改名
                except OSError as e:
                    return self._json({"ok": False, "error": f"重命名失败：{e}"})
                rel_norm = "/".join(p for p in rel.replace("\\", "/").split("/") if p and p != ".")
                new_rel = "/".join(rel_norm.split("/")[:-1] + [new_name])
                FAVS.rename_path(rel_norm, new_rel, new_name)   # 收藏夹里的路径同步迁移
                return self._json({"ok": True, "new_path": new_rel, "new_name": new_name})
            if route == "/api/favs":
                body = self._body_json()
                action = body.get("action")
                if action == "add":
                    ok = FAVS.add(str(body.get("path") or ""), str(body.get("fid") or ""))
                    return self._json({"ok": ok, "error": None if ok else "收藏失败"})
                if action == "remove":
                    ok = FAVS.remove(str(body.get("path") or ""), str(body.get("fid") or ""))
                    return self._json({"ok": True})
                if action == "newfolder":
                    entry = FAVS.new_folder(str(body.get("name") or ""))
                    if not entry:
                        return self._json({"ok": False, "error": "名称为空"})
                    return self._json({"ok": True, "entry": entry})
                if action == "delfolder":
                    ok = FAVS.del_folder(str(body.get("id") or ""))
                    return self._json({"ok": ok, "error": None if ok else "默认收藏夹不能删除"})
                if action == "renamefolder":
                    ok = FAVS.rename_folder(str(body.get("id") or ""), str(body.get("name") or ""))
                    return self._json({"ok": ok})
                return self._json({"ok": False, "error": "未知操作"})
            if route == "/thumb":
                self.put_thumb()
                return
            self._error(404, "Not Found")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self._error(500, f"服务器内部错误: {e}")
            except Exception:
                pass

    # ---------- 缩略图磁盘缓存：GET 取图 / POST 上传 ----------
    def get_thumb(self):
        fs_path = resolve(self._params().get("path", ""))
        if fs_path is None or not os.path.isfile(fs_path):
            return self._error(404, "no thumb")
        try:
            key = thumb_key(fs_path)
            with open(os.path.join(THUMB_DIR, key + ".jpg"), "rb") as f:
                data = f.read()
        except OSError:
            return self._error(404, "no thumb")
        dur = ""
        try:
            with open(os.path.join(THUMB_DIR, key + ".json"), encoding="utf-8") as f:
                dur = str(json.load(f).get("d") or "")
        except Exception:
            pass
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=604800")
        if dur:
            self.send_header("X-Dur", dur)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def put_thumb(self):
        params = self._params()
        fs_path = resolve(params.get("path", ""))
        if fs_path is None or not os.path.isfile(fs_path):
            return self._error(400, "bad path")
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > 400 * 1024:
            return self._error(400, "bad size")
        data = self.rfile.read(n)
        if not data.startswith(b"\xff\xd8"):     # 只接受 JPEG
            return self._error(400, "not jpeg")
        os.makedirs(THUMB_DIR, exist_ok=True)
        try:
            key = thumb_key(fs_path)
            tmp = os.path.join(THUMB_DIR, key + ".tmp")
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, os.path.join(THUMB_DIR, key + ".jpg"))
            try:
                d = float(params.get("d") or 0)
                with open(os.path.join(THUMB_DIR, key + ".json"), "w", encoding="utf-8") as f:
                    json.dump({"d": d, "p": os.path.abspath(fs_path)}, f, ensure_ascii=False)
            except Exception:
                pass
        except OSError as e:
            return self._error(500, f"save failed: {e}")
        self._json({"ok": True})

    # ---------- 视频流（支持 Range，手机端拖进度条必需） ----------
    def stream_video(self):
        fs_path = resolve(self._params().get("path", ""))
        if fs_path is None or not os.path.isfile(fs_path):
            return self._error(404, "文件不存在")
        ext = os.path.splitext(fs_path)[1].lower()

        # moov 在尾部的 mp4：用内存里的虚拟重排视图提供流，不写任何缓存文件
        fss = get_faststart(fs_path) if ext in FASTSTART_EXTS else None
        size = fss.size if fss is not None else os.path.getsize(fs_path)
        ctype = VIDEO_EXTS.get(ext, "application/octet-stream")

        start, end, status = 0, size - 1, 200
        range_header = self.headers.get("Range")
        if range_header and size > 0:
            m = re.match(r"bytes=(\d*)-(\d*)$", range_header.strip())
            if m:
                a, b = m.groups()
                if a == "" and b != "":            # bytes=-N 取末尾 N 字节
                    n = min(int(b), size)
                    start, end = size - n, size - 1
                elif a != "":
                    start = int(a)
                    end = int(b) if b else size - 1
                    if end >= size:
                        end = size - 1
                if start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                status = 206

        length = end - start + 1 if size > 0 else 0
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD" or length <= 0:
            return

        try:
            if fss is not None:
                with open(fs_path, "rb") as f:
                    for piece in iter_faststart_data(fss, f, start, length):
                        self.wfile.write(piece)
            else:
                with open(fs_path, "rb") as f:
                    f.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = f.read(min(CHUNK, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # 客户端中断（拖进度条/关闭页面）
        except Exception:
            pass  # 响应头已发出（如读盘被安全扫描阻塞后失败），只能断开连接
        print(f"[dbg] done T{threading.get_ident()} 实发={sent if 'sent' in dir() else '-'}", flush=True)


class VideoServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # 手机端拖进度条/离开页面/回收空闲连接时会中途断开，属正常现象，不刷屏
        # ConnectionError 覆盖 ConnectionReset/Aborted/BrokenPipe/Refused 四种
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError, socket.timeout)):
            return
        super().handle_error(request, client_address)


def find_free_port(start: int) -> int:
    for port in range(start, start + 20):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("0.0.0.0", port))
                return port
        except OSError:
            continue
    raise OSError("8080~8099 端口都被占用了，请用 --port 指定其他端口")


def lan_ips() -> list[str]:
    """找出本机所有局域网 IPv4 地址，供手机访问。"""
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0)
        s.connect(("192.168.255.255", 1))  # 不真正发包，只为拿到默认路由的出口 IP
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip.startswith(("192.168.", "10.", "172.")):
                ips.add(ip)
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith(("127.", "169.254.")))


# ================================================================ 控制台命令

HELP_TEXT = """
  命令：
    直接粘贴文件夹路径回车  添加该文件夹为播放目录
    ls                    查看当前播放目录列表
    rm 别名                移除某个目录（不删除文件）
    q                     停止服务并退出
    help                  显示本帮助
"""


def console_thread(server):
    """控制台交互线程：可以随时在这里添加/移除目录、退出服务。"""
    while True:
        try:
            line = input("视频站> ").strip()
        except (EOFError, OSError):
            return  # 无交互控制台（如后台运行）时静默退出
        if not line:
            continue
        low = line.lower()
        if low in ("q", "quit", "exit", "退出"):
            print("  正在停止服务…", flush=True)
            threading.Thread(target=server.shutdown, daemon=True).start()
            return
        if low in ("help", "?", "h", "帮助"):
            print(HELP_TEXT, flush=True)
            continue
        if low in ("ls", "dirs", "目录", "列表"):
            roots = LIB.roots()
            if not roots:
                print("  （还没有目录）", flush=True)
            for d in roots:
                print(f"  {d['alias']}  ←  {d['path']}", flush=True)
            continue
        if low.startswith(("rm ", "del ", "移除 ")):
            alias = line.split(None, 1)[1].strip()
            if LIB.remove(alias):
                LIB.save()
                print(f"  已移除：{alias}", flush=True)
            else:
                print(f"  没有叫「{alias}」的目录（用 ls 查看）", flush=True)
            continue
        # 其余输入当作路径处理
        p = line.strip('"').strip("'")
        if not os.path.exists(p):
            print("  无法识别的命令或路径不存在。输入 help 查看帮助。", flush=True)
        elif not os.path.isdir(p):
            print("  这不是一个文件夹。", flush=True)
        else:
            try:
                entry = LIB.add(os.path.abspath(p))
                LIB.save()
                print(f"  已添加：{entry['alias']}  ←  {entry['path']}", flush=True)
                if ARGS and not ARGS.no_warmup:
                    threading.Thread(target=warm_library([entry], f"新目录 {entry['alias']}"), daemon=True).start()
            except ValueError as e:
                print(f"  {e}", flush=True)


def main():
    global ARGS
    parser = argparse.ArgumentParser(description="局域网视频站")
    parser.add_argument("--dir", action="append", default=None, metavar="目录",
                        help="添加视频目录（可多次使用）")
    parser.add_argument("--port", type=int, default=8080, help="监听端口（默认 8080）")
    parser.add_argument("--no-open", action="store_true", help="启动后不自动打开浏览器")
    parser.add_argument("--no-warmup", action="store_true", help="关闭启动时的视频预热")
    ARGS = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    LIB.load(ARGS.dir)
    FAVS.load()
    port = find_free_port(ARGS.port)
    server = VideoServer(("0.0.0.0", port), Handler)

    ips = lan_ips()
    print()
    print("═" * 56)
    print("  🎬 局域网视频站 已启动")
    print("  播放目录:")
    for d in LIB.roots():
        print(f"      {d['alias']}  ←  {d['path']}")
    print(f"  本机访问  http://localhost:{port}/")
    if ips:
        print("  手机访问（手机和电脑连同一个 Wi-Fi）:")
        for ip in ips:
            print(f"      ➜  http://{ip}:{port}/")
    else:
        print("  未检测到局域网 IP，请检查网络连接")
    print("  停止服务  按 Ctrl+C 或输入 q")
    print("  添加目录  在下方输入框粘贴文件夹路径回车（help 查看命令）")
    print("═" * 56)
    print(flush=True)

    if not ARGS.no_open:
        threading.Timer(0.6, webbrowser.open, (f"http://localhost:{port}/",)).start()

    threading.Thread(target=console_thread, args=(server,), daemon=True).start()
    if not ARGS.no_warmup:
        threading.Thread(target=warm_library, args=(LIB.roots(), "启动预热"), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("\n已停止服务。")
        server.server_close()


if __name__ == "__main__":
    main()
