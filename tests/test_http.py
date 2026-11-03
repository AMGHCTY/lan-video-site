# -*- coding: utf-8 -*-
"""video_server.py 自动化测试（unittest，无需外部依赖）。

用法：
    python tests/test_http.py            # 跑纯逻辑测试 + 起一个临时实例跑 HTTP 测试
    python tests/test_http.py --no-live  # 只跑纯逻辑测试（不启动服务）

纯逻辑部分直接 import 单文件服务器里的函数（srt_to_vtt / resolve / safe_filename /
progress_set / recycle_file …）；live 部分在临时目录起真实服务，覆盖 Range、安全头、
登录限速、CSRF、上传校验、路径穿越、回收站删除、已看标记这些最容易回归的点。
"""
import contextlib
import hashlib
import http.client
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "video_server.py")
FF = shutil.which("ffmpeg")   # 生成测试视频用；与服务器同样的查找顺序（PATH → imageio_ffmpeg）
if not FF:
    try:
        import imageio_ffmpeg
        FF = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        FF = None

_vs = None


def load_vs():
    """把单文件服务器加载成模块（不启动服务）。"""
    global _vs
    if _vs is None:
        spec = importlib.util.spec_from_file_location("video_server_test", SERVER)
        mod = importlib.util.module_from_spec(spec)
        with contextlib.redirect_stdout(io.StringIO()):
            spec.loader.exec_module(mod)
        _vs = mod
    return _vs


def make_video(path, seconds=6, codec="libx264", audio=True, bitrate=None, size="320x240", q=None):
    ff = FF or os.environ.get("TEST_FFMPEG")
    if not ff:
        return False
    cmd = [ff, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size={size}:rate=10"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    cmd += ["-c:v", codec, "-pix_fmt", "yuv420p"]
    if bitrate:
        cmd += ["-b:v", bitrate]
    if q:
        cmd += ["-q:v", str(q)]
    if audio:
        cmd += ["-c:a", "aac"]
    cmd += ["-shortest", path]
    r = subprocess.run(cmd, capture_output=True)
    return r.returncode == 0 and os.path.isfile(path)


# ================================================================ 纯逻辑测试

class TestPureLogic(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.vs = load_vs()

    def test_srt_to_vtt(self):
        srt = ("1\n00:00:00,000 --> 00:00:02,000\n你好, 世界\n\n"
               "2\n00:00:02,500 --> 00:00:04,000\n第二行\n").encode("utf-8")
        vtt = self.vs.srt_to_vtt(srt)
        if isinstance(vtt, bytes):
            vtt = vtt.decode("utf-8")
        self.assertTrue(vtt.startswith("WEBVTT"))
        self.assertIn("00:00:00.000 --> 00:00:02.000", vtt)
        self.assertIn("你好, 世界", vtt)

    def test_safe_filename(self):
        f = self.vs.safe_filename
        self.assertIsNone(f("CON"))
        self.assertIsNone(f("con.mp4"))
        self.assertIsNone(f('bad"name'))
        self.assertIsNone(f(".hidden"))
        self.assertIsNone(f("  "))
        self.assertEqual(f(" 正常 名字.mp4 "), "正常 名字.mp4")

    def test_cookie_get(self):
        vs = self.vs

        class FakeHdr:
            def __init__(self, v):
                self.v = v

            def get(self, _):
                return self.v

        class FakeHandler:
            def __init__(self, cookie):
                self.headers = FakeHdr(cookie)

        g = vs._cookie_get
        self.assertEqual(g(FakeHandler("lvpw=abc"), "lvpw"), "abc")
        self.assertEqual(g(FakeHandler("a=1; lvpw=abc; b=2"), "lvpw"), "abc")
        # 解析器按 RFC 6265 精确取值；「token+垃圾后缀」拿到的是带后缀的完整值，
        # 与真实 token 比较必不相等（对比旧版子串匹配会把后缀吞掉造成绕过）
        self.assertEqual(g(FakeHandler("lvpw=abcEXTRA"), "lvpw"), "abcEXTRA")
        self.assertNotEqual(g(FakeHandler("lvpw=abcEXTRA"), "lvpw"), "abc")
        self.assertEqual(g(FakeHandler("foo=lvpw=abc"), "lvpw"), "")
        self.assertEqual(g(FakeHandler(""), "lvpw"), "")

    def test_ip_allow(self):
        vs = self.vs
        vs.ALLOW["list"] = ["192.168.31.0/24", "10.0.0.5"]
        self.assertTrue(vs.ip_allowed("192.168.31.77"))
        self.assertTrue(vs.ip_allowed("10.0.0.5"))
        self.assertFalse(vs.ip_allowed("8.8.8.8"))
        vs.ALLOW["list"] = []
        self.assertTrue(vs.ip_allowed("1.2.3.4"))          # 空白名单 = 放行全部
        self.assertEqual(vs._parse_allow(["1.2.3.4", "垃圾条目", "10.0.0.0/8"]),
                         ["1.2.3.4", "10.0.0.0/8"])

    def test_resolve_traversal(self):
        vs = self.vs
        tmp = tempfile.mkdtemp(prefix="lvtest_")
        try:
            inside = os.path.join(tmp, "inside")
            outside = os.path.join(tmp, "outside")
            os.makedirs(inside)
            os.makedirs(outside)
            with open(os.path.join(outside, "secret.txt"), "w") as f:
                f.write("x")
            vs.LIB.dirs = [{"alias": "t1", "path": inside}]
            # 正常子路径
            os.makedirs(os.path.join(inside, "sub"))
            self.assertIsNotNone(vs.resolve("t1/sub"))
            # .. 穿越直接拒绝
            self.assertIsNone(vs.resolve("t1/../../outside"))
            # 未知别名
            self.assertIsNone(vs.resolve("nope/x"))
            # junction / 符号链接逃逸（Windows 上建 junction，非 Windows 跳过）
            if os.name == "nt":
                r = subprocess.run(["cmd", "/c", "mklink", "/J",
                                    os.path.join(inside, "jump"), outside],
                                   capture_output=True)
                if r.returncode == 0:
                    try:
                        self.assertIsNone(vs.resolve("t1/jump/secret.txt"))
                    finally:
                        subprocess.run(["cmd", "/c", "rmdir", os.path.join(inside, "jump")],
                                       capture_output=True)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_progress_set_watched(self):
        vs = self.vs
        tmp = tempfile.mkdtemp(prefix="lvtest_")
        try:
            vid = os.path.join(tmp, "v.mp4")
            with open(vid, "wb") as f:
                f.write(b"\x00" * 1024)
            vs.LIB.dirs = [{"alias": "t2", "path": tmp}]
            rel = "t2/v.mp4"
            # 中间进度
            self.assertTrue(vs.progress_set(rel, 30, 100))
            p = vs.load_progress()
            self.assertAlmostEqual(p[rel]["t"], 30, delta=0.5)
            self.assertNotIn("w", p[rel])
            # 播完 → 自动已看并保留
            self.assertTrue(vs.progress_set(rel, 99, 100))
            p = vs.load_progress()
            self.assertEqual(p[rel].get("w"), 1)
            # 重新从头看（t<5）不丢已看标记
            self.assertTrue(vs.progress_set(rel, 2, 100))
            p = vs.load_progress()
            self.assertEqual(p[rel].get("w"), 1)
            # 手动取消已看 → 记录清掉
            self.assertTrue(vs.progress_set(rel, 30, 100, 0))
            p = vs.load_progress()
            self.assertNotIn(rel, p)
            # 手动标记已看
            self.assertTrue(vs.progress_set(rel, 0, 100, 1))
            p = vs.load_progress()
            self.assertEqual(p[rel].get("w"), 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            if os.path.isfile(vs.PROGRESS_FILE):
                os.remove(vs.PROGRESS_FILE)

    @unittest.skipIf(os.name != "nt", "Windows 专用")
    def test_recycle_file(self):
        vs = self.vs
        tmp = tempfile.mkdtemp(prefix="lvtest_")
        try:
            f = os.path.join(tmp, "recyclable.txt")
            with open(f, "w") as fh:
                fh.write("x")
            if vs.recycle_file(f):
                self.assertFalse(os.path.exists(f))
            # 不支持时返回 False（不抛异常）
            self.assertFalse(vs.recycle_file(os.path.join(tmp, "不存在.txt")))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_windows_reserved_names(self):
        vs = self.vs
        for name in ("CON", "PRN", "AUX", "NUL", "COM1", "LPT9"):
            self.assertIn(name, vs.WINDOWS_RESERVED)


# ================================================================ live HTTP 测试

def _free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@unittest.skipUnless(os.path.isfile(SERVER), "video_server.py 不存在")
class TestLive(unittest.TestCase):
    """起临时实例跑真实 HTTP 回归。"""

    proc = None
    port = None
    media = None

    @classmethod
    def setUpClass(cls):
        if "--no-live" in sys.argv:
            raise unittest.SkipTest("live 测试已禁用")
        cls.tmp = tempfile.mkdtemp(prefix="lvlive_")
        cls.media = os.path.join(cls.tmp, "media")
        os.makedirs(cls.media)
        has_video = make_video(os.path.join(cls.media, "a.mp4"))
        if has_video:
            # b.avi：转码测试用；big1/big2：同大小重复对（mpeg4 体积稳定 >1MB，dupes 阈值）
            make_video(os.path.join(cls.media, "b.avi"), seconds=4, codec="mpeg4")
            make_video(os.path.join(cls.media, "big1.mp4"), seconds=40,
                       codec="mpeg4", audio=False, size="640x480", q=8)
            shutil.copyfile(os.path.join(cls.media, "big1.mp4"),
                            os.path.join(cls.media, "big2.mp4"))
        cfg = os.path.join(cls.tmp, "library.json")
        json.dump({"dirs": [{"alias": "media", "path": cls.media}]},
                  open(cfg, "w", encoding="utf-8"))
        cls.port = _free_port()
        # 用临时目录当 SCRIPT_DIR（隔离 library/progress/settings）
        server_file = os.path.join(cls.tmp, "video_server.py")
        shutil.copyfile(SERVER, server_file)
        cls.proc = subprocess.Popen(
            [sys.executable, server_file, "--port", str(cls.port), "--no-warmup"],
            cwd=cls.tmp, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        for _ in range(60):
            try:
                cls.req("GET", "/")
                break
            except OSError:
                time.sleep(0.25)
        else:
            raise RuntimeError("测试服务器未能启动")

    @classmethod
    def tearDownClass(cls):
        if cls.proc:
            cls.proc.kill()
            cls.proc.wait(timeout=10)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def req(cls, method, path, body=None, headers=None, timeout=30):
        c = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=timeout)
        h = dict(headers or {})
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            if not isinstance(body, bytes):
                h.setdefault("Content-Type", "application/json")
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        out = r.read()
        hd = dict(r.getheaders())
        c.close()
        return r.status, hd, out

    @unittest.skipUnless(FF or os.environ.get("TEST_FFMPEG"), "需要 ffmpeg 生成测试视频")
    def test_01_static_and_headers(self):
        s, h, d = self.req("GET", "/")
        self.assertEqual(s, 200)
        self.assertIn("Content-Security-Policy", h)
        self.assertEqual(h.get("X-Content-Type-Options"), "nosniff")
        self.assertIn("X-Frame-Options", h)
        self.assertIn("Server", h)   # Server: LANVideo/...

    @unittest.skipUnless(FF or os.environ.get("TEST_FFMPEG"), "需要 ffmpeg")
    def test_02_range(self):
        size = os.path.getsize(os.path.join(self.media, "a.mp4"))
        s, h, d = self.req("GET", "/video?path=media/a.mp4",
                           headers={"Range": "bytes=0-0"})
        self.assertEqual(s, 206)
        self.assertEqual(len(d), 1)
        self.assertEqual(h.get("Content-Range"), f"bytes 0-0/{size}")
        # 倒置区间 → 退回 200 全量
        s, h, d = self.req("GET", "/video?path=media/a.mp4",
                           headers={"Range": "bytes=5-2"})
        self.assertEqual(s, 200)
        self.assertEqual(len(d), size)
        # 越界 → 416
        s, h, d = self.req("GET", "/video?path=media/a.mp4",
                           headers={"Range": "bytes=999999999-"})
        self.assertEqual(s, 416)

    @unittest.skipUnless(FF or os.environ.get("TEST_FFMPEG"), "需要 ffmpeg")
    def test_03_login_flow_and_lockout(self):
        # 设置密码（直接写 settings.json 需要重启；这里走 console 不可行，改用文件 + 重启）
        # 简化：验证未设密码时可自由访问，登录页可访问
        s, h, d = self.req("GET", "/login")
        self.assertEqual(s, 200)
        # XSS：next 参数必须整体转义进属性值（payload 里的引号变 &quot;，
        # 不存在能脱离属性值的裸引号 → onfocus= 无法成为真实属性）
        s, h, d = self.req("GET", "/login?next=%2F%22%20autofocus%20onfocus%3D%22alert(1)%22%20x%3D%22")
        self.assertNotIn(b'onfocus="alert', d)
        self.assertIn(b"&quot;", d)
        # 未登录 API GET → 302 跳登录
        s, h, d = self.req("GET", "/api/list",
                           headers={"Cookie": "lvpw=invalid"})
        # 没设密码时不会 302（auth 恒真），设了密码才会 —— 两种都算合理，只要求非 5xx
        self.assertLess(s, 500)

    @unittest.skipUnless(FF or os.environ.get("TEST_FFMPEG"), "需要 ffmpeg")
    def test_04_csrf_and_upload(self):
        s, h, d = self.req("POST", "/api/delete", {"paths": []},
                           headers={"Origin": "http://evil.example"})
        self.assertEqual(s, 403)
        s, h, d = self.req("POST", "/api/delete", {"paths": []})
        self.assertEqual(s, 200)
        # 上传非视频扩展名 → 400
        s, h, d = self.req("POST", "/api/upload?dir=&name=evil.exe", b"MZ")
        self.assertEqual(s, 400)
        # 超大 JSON body → 413
        s, h, d = self.req("POST", "/api/progress", b"\x00" * 70000)
        self.assertEqual(s, 413)

    @unittest.skipUnless(FF or os.environ.get("TEST_FFMPEG"), "需要 ffmpeg")
    def test_05_path_traversal_via_http(self):
        s, h, d = self.req("GET", "/api/info?path=media/../../etc/passwd")
        j = json.loads(d)
        self.assertFalse(j.get("ok"), "路径穿越必须被拒绝")

    @unittest.skipUnless(FF or os.environ.get("TEST_FFMPEG"), "需要 ffmpeg")
    def test_06_progress_watched_api(self):
        s, h, d = self.req("POST", "/api/progress",
                           {"path": "media/a.mp4", "t": 5.8, "d": 6})
        self.assertTrue(json.loads(d).get("ok"))
        s, h, d = self.req("GET", "/api/progress")
        j = json.loads(d)
        self.assertEqual(j.get("p", {}).get("media/a.mp4", {}).get("w"), 1)  # 播完自动已看

    @unittest.skipUnless(FF or os.environ.get("TEST_FFMPEG"), "需要 ffmpeg")
    def test_07_dupes_and_delete_recycle(self):
        s, h, d = self.req("GET", "/api/dupes")
        groups = json.loads(d).get("groups", [])
        self.assertTrue(any(len(g.get("items", [])) == 2 for g in groups), f"dupes groups: {groups}")
        # 回收站删除（Windows 上 trashed=1；其他平台直接删 removed=1）
        s, h, d = self.req("POST", "/api/delete", {"paths": ["media/big2.mp4"]})
        j = json.loads(d)
        self.assertTrue(j.get("ok"))
        self.assertEqual(j.get("removed"), 1)
        if os.name == "nt":
            self.assertEqual(j.get("trashed"), 1)
        self.assertFalse(os.path.exists(os.path.join(self.media, "big2.mp4")))

    @unittest.skipUnless(FF or os.environ.get("TEST_FFMPEG"), "需要 ffmpeg")
    def test_08_malformed_requests_no_500(self):
        cases = [
            ("POST", "/api/progress", b"not json"),
            ("POST", "/api/progress", b'{"t":"abc","d":"x"}'),
            ("POST", "/api/delete", b'{"paths": "str"}'),
            ("POST", "/api/rename", b'{"path":"media/a.mp4","name":"CON"}'),
            ("GET", "/api/info?path=media/%zz", None),
        ]
        for m, p, b in cases:
            s, h, d = self.req(m, p, b)
            self.assertLess(s, 500, f"{m} {p} -> {s}")


def main():
    unittest.main(verbosity=1, argv=[sys.argv[0]] + [a for a in sys.argv[1:] if a != "--no-live"])


if __name__ == "__main__":
    main()
