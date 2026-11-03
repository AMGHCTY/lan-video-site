# 代码评审报告 · 局域网视频站

- 评审对象：本项目原始版本（单文件 `video_server.py`）
- 原始版本：v1.2.0（`video_server.py` 4273 行 / 184 KB，单文件）
- 第一轮（安全与健壮性修复）：4499 行 / 195 KB，+287 / −61 行
- 第二轮（功能需求与 UI 调整）：4808 行 / 205 KB
- 第三轮（播放浮层自动隐藏）：4808 行 / 210 KB
- 第四轮（缩略图批处理 + 播放记录页 + 分速隐藏）：5079 行 / 222 KB
- 版本号按用户要求统一为 **v1.2.0**（不再随每次改动递增）
- 原始文件备份：`git show cbb714f:video_server.py`（本文件即基于该版本）
- 验证方式：真实启动服务 + 真实 ffmpeg 转码 + HTTP 抓包复现 + headless 浏览器截图，逐条回归

---

## 一、总体评价

这个项目的完成度相当高，不是玩具级 demo：

- **虚拟 faststart 重排**是最亮眼的设计——在内存里把 moov 提前并修正 stco 偏移，不写缓存、不改原文件，就能让 moov 在尾部的 mp4 秒拖进度条。这部分实现正确（实测 `ftyp(32) → moov(6453)` 且偏移已修正），很多同类项目直接放弃或粗暴转码。
- **实时转码推流**用分片 MP4（`+empty_moov+default_base_moof+frag_keyframe`）边转边发，点开即播，比"转完再播"体验好一个量级。
- **硬件编码器探测**（NVENC → QSV → AMF → x264）带真实编码测试而非只看名字，务实。
- 前端是手写原生 JS，没有框架负担，PWA / MediaSession / IndexedDB 三级缩略图缓存 / 手势操作都做了，移动端细节考虑得很细。
- 中文文案统一、UI 完整、控制台命令齐全，作为自用工具已经超出预期。

主要问题集中在**安全边界**和**边界条件处理**上——这也是单人快速迭代最容易漏的部分。以下按严重程度排列。

---

## 二、已修复的问题（第一轮 · 安全加固）

### 🔴 严重

#### 1. 反射型 XSS（登录页 `next` 参数未转义）

`send_login_page()` 把 query 里的 `next` 直接 `str.replace` 进 HTML：

```python
page = LOGIN_PAGE.replace("__NEXT__", nxt)   # nxt 完全未转义
```

而模板里是 `<input type="hidden" name="next" value="__NEXT__">`。请求

```
GET /login?next=/" autofocus onfocus="document.title='XSS'" x="
```

即可注入属性并**零交互执行任意 JS**（`autofocus` + `onfocus`）。更糟的是 `/login` 在 `auth_ok` 之前处理、且在免认证白名单里，**没设密码时同样可打**，等于任何能访问到该端口的人都能在受害者浏览器里执行脚本。

**修复**：`html_escape(nxt, quote=True)`，并把 `next` 校验补上 `startswith("/\\")` 归 `/`。

#### 2. 认证 Cookie 用子串匹配，可绕过

```python
if ("lvpw=" + auth_token()) in (handler.headers.get("Cookie") or ""):
```

`in` 是子串匹配，所以下面两种畸形 Cookie 都能通过：

| Cookie | 修复前 | 修复后 |
| --- | --- | --- |
| `lvpw=<token>` | 200 | 200 |
| `lvpw=<token>EXTRA` | **200** | 302 |
| `foo=lvpw=<token>` | **200** | 302 |
| `a=1; lvpw=<token>; b=2` | 200 | 200 |

单独看不算致命（攻击者仍需知道 token），但它意味着任何能把 token 塞进任意 cookie 值的路径都成了后门，且让"精确校验"这个前提失效。

**修复**：新增 `_cookie_get(handler, name)` 按 `;` 分割 + `partition("=")` 精确解析，再用 `hmac.compare_digest` 常量时间比较。

#### 3. 登录零防护 + 弱哈希

- 20 次错误密码 POST 总耗时 **690 ms**（34 ms/次），无延迟、无锁定、无验证码。
- `set_password()` 只做单轮 `sha256(salt + pw)`，salt 16 字节 hex。GPU 下 8 位密码几分钟可破。

**修复**：
- 改用 `hashlib.pbkdf2_hmac("sha256", pw, salt, 200_000)`，`settings.json` 增加 `rounds` 字段。
- 旧格式（`rounds == 0`）仍可登录，**成功后自动升级**为新格式，不破坏已有用户。
- 同 IP 连续 8 次失败锁定 5 分钟，期间即使密码正确也返回 429。

#### 4. 路径穿越（junction / 符号链接逃逸）

`resolve()` 原本是：

```python
target = os.path.abspath(os.path.join(root, *parts[1:]))
if not target.startswith(root + os.sep):
    return None
```

`abspath` 不做符号链接解析，所以建一个指向库外的 junction 就能读任意文件。实测：

```
mklink /J .testmedia\jump .testrun\outside
GET /video?path=测试/jump/secret.mp4   → 200，完整下载库外 186856 字节
GET /api/list                          → jump 被当普通目录列出
GET /api/search?q=secret               → 能搜到库外文件
```

**修复**：改用 `os.path.realpath` + `os.path.normcase` + `os.path.commonpath`（`ValueError` 兜住跨盘符）。修复后同一请求返回 404。

顺带修掉一个隐蔽 bug：把整个盘符根加为库时，`os.path.abspath("D:\\")` = `D:\`，而 `root + os.sep` = `D:\\`（双斜杠），导致 `D:\foo.mp4` 的前缀判断为 False——**盘符根作库时所有子路径都失效**。`commonpath` 方案同时解决这个问题。

#### 5. CSRF（无任何来源校验）

带正确 cookie 的跨站请求可直接删文件：

```http
POST /api/delete HTTP/1.1
Origin: http://evil.example
Cookie: lvpw=<token>
{"paths":["测试/evil.exe"]}
→ {"ok": true, "removed": 1}
```

`SameSite=Lax` 只挡跨站 POST 的表单场景，挡不住 `fetch` 在部分场景或同站子域的攻击面，且 `Lax` 对顶层 GET 导航仍放行。

**修复**：新增 `_same_origin()`，`POST` 一律校验 `Origin`/`Referer` 的 netloc 与 `Host` 一致，不匹配返回 403。curl 等不带这两个头的客户端仍放行（不破坏脚本化使用）。

### 🟠 中等

#### 6. Range 倒置 → 损坏的 206 响应

`Range: bytes=5-2` 时 `length = end - start + 1 = -2`，而 `Content-Length` 在 `end_headers()` 之前已按 `-2` 写出。curl 直接报 `* Invalid Content-Length: value` 并断开。`bytes=10-9` 则返回 `206 / Content-Length: 0`（同样不合规）。

**修复**：用局部 `rng` 变量，只有 `e0 >= s0` 时才设 206；倒置区间整段忽略、退回 200 全量。回归结果：

| Range | 修复前 | 修复后 |
| --- | --- | --- |
| `bytes=0-0` | 206 / CL:1 | 206 / CL:1 / `bytes 0-0/186856` |
| `bytes=-5` | 206 / CL:5 | 206 / CL:5 / `bytes 186851-186855/186856` |
| `bytes=0-` | 206 / CL:186856 | 206 / CL:186856 |
| `bytes=5-2` | **206 / CL:-2（损坏）** | 200 / CL:186856 |
| `bytes=10-9` | **206 / CL:0** | 200 / CL:186856 |
| `bytes=999999999-` | 416 | 416 / CL:0 / `bytes */186856` |

#### 7. 超大请求体污染 keep-alive

`_body_json()` 在 `n > 65536` 时直接 `return {}`，**不读 rfile**。残留的 70000 字节被 basehttp 当成下一个请求行解析：

```
POST 70001 字节 → 200 {"ok": false}
同一连接紧接着 GET → 414 Request-URI Too Long
```

**修复**：新增 `_drain(n, cap=8MB)` 排空后 `close_connection = True` 并抛 `BodyTooLarge`，`do_POST` 捕获后回 413。实测现在返回 `413 {"ok": false, "error": "请求体过大"}`，连接由服务端关闭。

#### 8. 无音轨视频被白白重编码

```python
a_ok = probe.get("a") in BROWSER_AUDIO
```

无音轨时 `probe["a"]` 是 `"?"`，不在集合里 → 判定"音频不兼容" → `transcode_audio`。**所有静音视频都多跑一路 ffmpeg**，在低配机器上是实打实的浪费。

**修复**：`a_ok = probe.get("a") in BROWSER_AUDIO or probe.get("a") == "?"`。实测 `fast.mp4`（h264/mp4 无音轨）从 `transcode_audio` 变为 `direct`。

#### 9. fps 解析错误

```python
re.search(r"(\d+(?:\.\d+)?)\s*fps", line)
```

对 `30000/1001 fps` 取到 **1001**（分子被吃掉），`24000/1001` 同样。影响 `/api/info` 展示和"最长/帧率"排序。

**修复**：先匹配 `(\d+)/(\d+)\s*fps` 做除法，失败再退回小数匹配。实测 `30000/1001 → 29.97`、`24000/1001 → 23.976`、`25 → 25`。

#### 10. 错误信息泄露

```python
self._error(500, f"服务器内部错误: {e}")
```

实测把 `could not convert string to float: 'abc'`、`[WinError 3] 系统找不到指定的路径。: '\\\\?\\Z:\\'` 等原文回给客户端，泄露服务端路径与内部状态。

**修复**：异常详情只 `print` 到服务端日志，回客户端统一"服务器内部错误"。同时修掉 `/api/progress` 等端点对畸形 body 抛 500 的问题（现在全部 200/400）。

#### 11. 上传无任何校验

`put_upload` 只靠前端 `accept` 过滤，后端不校验。实测上传 `evil.exe`（15 字节）成功落盘。另外没有体积上限，一个超大文件就能塞满磁盘。

**修复**：扩展名白名单（不在 `VIDEO_EXTS` 里返回 400「只能上传视频文件」）+ `MAX_UPLOAD_BYTES = 32 GB` 上限（超出返回 413）。

#### 12. 无任何安全响应头

`/` 响应里 CSP、`X-Content-Type-Options`、`X-Frame-Options`、`Referrer-Policy` 全都没有。虽然内网风险有限，但既然页面本身有大量动态内容，加一层纵深防御成本极低。

**修复**：覆写 `Handler.end_headers()` 统一注入四个头（含完整 CSP），用 `_security_headers_sent` 防止重复发送。

### 🟡 轻微

| # | 问题 | 修复 |
| --- | --- | --- |
| 13 | `_params()` 手写 query 解析，`?q=a+b` 搜不到（`+` 未转空格） | 改用 `urllib.parse.parse_qs(q, keep_blank_values=True)` |
| 14 | Windows 保留名可作文件名（实测 `CON` 改名成功） | 新增 `safe_filename()`，`WINDOWS_RESERVED` 覆盖 CON/PRN/AUX/NUL/COM1-9/LPT1-9 |
| 15 | `lan_ips()` 用 `172.` 前缀，把公网 172.x 也当内网 | 改为 `172.16`–`172.31` |
| 16 | 版本号硬编码三处（页头 / 横幅 / `Server:`） | 统一为 `VERSION` 单一来源 |
| 17 | `_FS_BUILD_LOCKS` per-path Lock 永不清理 | 淘汰 `_FS_CACHE` 条目时同步 `pop` 对应 Lock |
| 18 | `_PROBE_CACHE`/`_COUNT_CACHE`/`_COVER_CACHE`/`_THUMB_FAIL` 无上限 | 新增 `_cache_put()`，超 4000 条清最旧一半 |
| 19 | 前端 `wideSection()` 用 `innerHTML` 拼标题 | 改为 `createElement` + `textContent` |

---

## 二·五、第二轮改动（需求实现）

### 🔴 `port` 换端口后新旧端口都连不上（功能性 BUG）

**根因**：原实现在控制台线程里直接 `VideoServer(("0.0.0.0", new_port), ...)` 创建新 server，然后 `shutdown()` 旧 server。但新 server 的 `serve_forever()` 从未被调用——它只是被创建、赋值给 `_NEXT_SERVER`，然后线程继续循环，主循环仍在旧 server 的 `serve_forever()` 上。旧 server 被 shutdown 后主循环退出，程序要么结束要么空转，于是**新端口没在监听、旧端口已经关闭**。

**修法**：改成"登记 + 主线程接管"。
- `_SWITCH = {"want": None}` 只记录目标端口，控制台线程打印提示后 `old.shutdown()`；
- 主循环 `serve_forever()` 返回后检查 `_SWITCH["want"]`，`server_close()` 旧 server，**在主线程**新建 `VideoServer` 并继续 `serve_forever()`；
- 新端口绑定失败时自动回退原端口，打印「端口 N 无法监听：e，已退回原端口 M」。

实测连续切换 8899 → 8902 → 8903 → 8901 全部成功，旧端口立即 closed、新端口 200。

### 🟠 IP 白名单（`allow`）

`ipaddress.ip_network(s, strict=False)` 校验 + `addr in net` 判定，支持单个 IP 与 CIDR，逗号/空格/中文逗号分隔，非法条目跳过并提示。规则存 `settings.json` 的 `allow` 字段，重启生效。空白名单 = 放行全部。

`Handler._ip_blocked()` 在 `do_HEAD`/`do_GET`/`do_POST` 入口统一拦截，命中返回 403 + `Connection: close`。

实测：`allow 10.0.0.0/8` → 本机 403；`allow off` → 200；`allow 127.0.0.1,192.168.31.0/24` → 200；非法条目被忽略且不影响有效条目；重启后规则仍生效。

### 🟠 播放记录（原「继续观看」）

三个问题一起修：
1. **失效记录不自动删除**：`progress_validated()` 把源文件已不存在的键收进 `gone` 列表（模块级 `_PURGED` + `progress_take_purged()` 一次性取出），`/api/progress` 返回 `{"p": {...}, "gone": [...]}`，前端 `progReady` 里 `(d.gone||[]).forEach(k=>{delete p[k]; delete srvProg[k];})` 同步清掉本地缓存。这样记录不会**凭空消失**（仍会报给前端），但前端不再显示「1 KB / 1970-01-01」的空卡片。
2. **无法主动清空**：新增 `POST /api/progress/clear`、首页「清空记录」按钮（`clearHistory()`：confirm → 清 localStorage + `srvProg` + 逐条 `postProgress(k,0,0)` → 调接口 → toast）、控制台 `history clear`。
3. **命名**：`▶ 继续观看` → `▶ 播放记录`。

另修 `videoCard` 容错：`size`/`mtime` 缺失时不渲染对应 meta span，避免失效记录显示 `0 B` / `1970-01-01`。

### 🟡 播放页控件收敛

原来倍速/画质/截图/练习铺成页面下方两行按钮（手机截图里那块红圈），现在全部收进视频右上角的 **⚙ 播放设置** 面板：

- `<button id="tset">`（内联 SVG 齿轮，`aria-label="播放设置"`，`display:flex` 居中，绝对定位 `top:calc(12px + safe-area-inset-top); right:14px`）；
- `<div id="tsheet">` 面板：`bottom:52px`，`rgba(16,18,24,.985)` + `blur(18px)`，`max-height:calc(100% - 70px)` + `overflow-y:auto`（矮屏可滚），宽屏（≥560px）收成 `width:352px` 靠右小面板；
- `buildPlayerRows()` 渲染倍速行 / 画质行 / 字幕行 / 工具行（📸截图、🎯练习）/ 练习子行 / 收起；
- 点击 `#tset` 切换（`stopPropagation`），`document` 捕获阶段点面板外收起；转码手势 `grab` 选择器扩到 `#tbar,#tsheet,#tset,#back,button`，避免拖进度条时误触；
- `practiceOn` 从 DOM `style.display` 判断改为模块级 `let` 状态。

删除的旧节点/函数：`#prow1`、`#practiceRow`、`paintPractice()`。

### 🟡 文案与容错

- `⬆ 上传视频到这里` → `⬆ 上传视频`（两处）
- `✓ 管理` / `✕ 退出管理` → `管理` / `退出管理`
- `LIB.load()` 容错裸数组：原 `data.get("dirs", [])` 遇到 `library.json` 是裸数组时崩 `AttributeError: 'list' object has no attribute 'get'`；改为先判 `isinstance(data, list)`，再兜 `isinstance(dirs, list)`。

### 验证记录（第二轮 · 功能需求）

| 项目 | 结果 |
| --- | --- |
| 版本一致性 | 横幅 / `Server:` 头 / 页头 三处版本号一致 |
| 端口切换 | 8899→8902→8903→8901 全部成功，旧端口立即 closed |
| 白名单 | 10/8 → 403；`off` → 200；双条目 → 200；非法条目被忽略；重启后仍生效 |
| 播放记录 | `gone` 正确上报；`POST /api/progress/clear` → `{"ok": true}`；首页「播放记录 1 条」+「清空记录」 |
| 播放设置面板 | 齿轮按钮渲染正常；面板展开/收起正常；宽屏收窄为 352px；矮屏可滚动 |
| 旧标识残留 | `prow1` / `practiceRow` / `paintPractice` / `上传视频到这里` / `✓ 管理` / `继续观看` 均为 0 处 |
| JS 语法 | 两个 `<script>` 块（318 B + 65 KB）`node --check` 通过 |
| 端点冒烟 | `/`、`/api/list`、`/api/info`、`/api/progress`、`/api/favs`、`/api/dupes`、`/api/subs`、`/api/search` 全部 200 |
| 控制台 | `allow`（设置/查看/非法/关闭）、`history`（查看/清空）、`port` 全部按预期输出 |

> 截图验证时踩到两个坑：
> 1. headless Edge 的窗口宽度**下限是 504px**，`--window-size=430,932` 实际渲染宽度仍是 504，截图只截到左边 430px，看起来像"齿轮按钮被裁掉了"。用 `--dump-dom` 打出 `innerWidth=504 / tset=450,75,40,40` 才确认布局本身没问题。
> 2. `--virtual-time-budget` 下 CSS `transition` 不一定推进，直接截会拍到 `opacity` 的中间值。验证时临时注入 `#back,#tset{transition:none !important}` 读最终值（`opacity=0 / pointer-events:none`）才确认生效。

### 🟡 播放浮层自动隐藏（第三轮）

用户反馈播放画面上「←」和「⚙」两个圆钮一直挂着挡画面。改为播放中自动淡出：

- CSS：`.vwrap.chromehide #back,.vwrap.chromehide #tset{opacity:0;transform:scale(.86);pointer-events:none}`（`pointer-events:none` 保证隐藏后不会误点到不可见的按钮）
- JS：`chromeShow/chromeHide/chromeWake/chromeStop` 四个函数 + `chromeHidden`/`chromeTimer`/`chromeLastWake` 状态
- 触发时机：`v.onplay` → `chromeWake()`（2.5 秒后淡出）；`v.onpause` → `chromeStop()`（立刻显示并停表）；`stopPlayback()` → `chromeStop()`
- 唤醒：`.vwrap` 上的 `pointerdown`/`pointermove`/`keydown`、`document` 的 `mousemove`、`#tsheet` 的 `pointerdown`，全部 `{passive:true}`
- 面板开着时不隐藏：`#tset` 打开面板时 `clearTimeout(chromeTimer)`，收起后 `chromeWake()` 重新计时
- `chromeWake()` 内加 200 ms 节流（`chromeLastWake`），避免拖动进度条时 `pointermove` 每帧都重置计时器

实测：播放中 `cls=vwrap chromehide`，`#back`/`#tset` 的 `opacity=0`、`pointer-events=none`；派发一次 `pointerdown` 后 `cls=vwrap`、`opacity=1`、`pointer-events=auto`；暂停后按钮常驻。

> 这一轮的"两个按钮同一个计时器"在第四轮被拆成两条独立计时（见下），`chromehide` / `chromeTimer` / `chromeHidden` 三个标识现已全部移除。

---

## 二·六、第四轮改动（缩略图批处理 · 播放记录页 · 分速隐藏）

### 🟢 控制台 `thumb`：一次性补齐全部缩略图

用户发现 `.thumbcache` 里只有 107 张图，而库里有几百个视频。原因是缩略图**按需生成**——只有被浏览到的视频才会截帧（`ensure_thumb()` 在 `/thumb` 请求里现场生成，一次生成永久缓存）。这是合理的设计，但首次打开一个大库时会看到一片 🎞️ 占位图。

新增控制台命令：

- `thumb` / `thumbs` / `缩略图` / `封面` → `threading.Thread(target=gen_all_thumbs, args=(roots, "手动"), daemon=True)`
- `thumb stop` / `cancel` / `停止` / `中断` → 置 `_THUMB_BATCH["stop"] = True`，循环里检查后退出
- 已在跑时再敲 `thumb` → 提示「已有缩略图任务在跑，用 thumb stop 可以中断」

`gen_all_thumbs()` 的要点：

- `os.walk` 收集全部视频，跳过 `.` / `$` 开头的目录（回收站、系统目录）
- 逐个 `thumb_key(p)`，已有 `THUMB_DIR/key.jpg` 就 `skipped += 1` **直接跳过**（可重复执行，增量补齐）
- 没有则先 `_THUMB_FAIL.pop(key, None)` 清掉失败冷却——上次失败可能只是磁盘休眠或文件被占用，不该永久跳过
- 复用 `_THUMB_GEN_SEM`，与网页请求共用同一并发闸门，不会为了生成缩略图把磁盘打满
- 每 20 个打印一次进度「进度 i/total · 新生成 X · 已有 Y · 失败 Z」，结束打印汇总 + 用时

实测：4 个视频 → 「新生成 4 个，已有 0 个，失败 0 个，用时 0 秒」；再跑一次 → 「新生成 0 个，已有 4 个」（幂等）；加两个坏文件（`broken.mp4` 内容不是视频、`empty.mp4` 0 字节）后 111 个视频 → 「新生成 6 个，已有 103 个，失败 2 个」，两个坏文件确认没生成 jpg。

### 🟢 播放记录独立页面（`#/history`）

用户要求：单独的播放记录页，**页面显示上限 100 条**，点首页「▶ 播放记录」标题进入。

- `wideSection(title, sub, href)` 增加第三个参数：传了 `href` 就加 `.seclink` 类（`cursor:pointer` + hover 高亮 + 尾部 `›` 箭头），绑 `click` 与 `keydown`（Enter/Space）都跳 `location.hash = href`
- 首页那块仍然只渲染 12 张卡片（保持原有节奏），但标题变成可点的入口
- 新路由 `#/history` → `openHistory()`；`HISTORY_PAGE_LIMIT = 100` 只是**这一页显示多少条**，`progress.json` 里仍保留全部记录（服务端上限 2000 条）
- 超限时副标题显示 `100 / 105 条`，列表末尾追加「本页最多显示 100 条，还有 N 条更早的记录未显示」
- 每行：缩略图 + 标题 + 「时长 · 日期 看过 · 大小」+ 进度条 + 「看到 0:43 / 共 2:00」+ 右侧 ✕ 单独移除
- ✕ 走 `stopPropagation()`，删 localStorage + `delete srvProg[path]` + `postProgress(path, 0, 0)` + `row.remove()`，不会误触发行点击跳转

**踩到两个真 bug**：

1. **`navSeq` 竞态**：`openHistory()` 若只是 `await progReady` 后直接渲染，会被同时进行的 `openDir()` 覆盖——深链打开 `#/history` 时先渲染出播放记录、随后首页的 `openDir` 完成把它盖成首页。修法是 `const seq = ++navSeq;` … `if(seq !== navSeq) return;`，与 `openDir` 共用同一个序号。
2. **CSS 类名撞车**：新写的行样式最初叫 `.hrow`，与页头既有的 `.hrow{display:flex;align-items:center;gap:12px;max-width:1200px;margin:0 auto}` 同名，导致页头被套上卡片边框、`document.querySelectorAll(".hrow")` 多出一个。全部改成 `hist-` 前缀（`.hist-list` / `.hist-row` / `.hist-thumb` / `.hist-info` / `.hist-title` / `.hist-meta` / `.hist-bar` / `.hist-left` / `.hist-del` / `.hist-more`）。

实测：`rows=100`、`headerRow=1`（页头 `hrow` 恢复正常）、`sub=100 / 105 条`、首行 `meta=2:00 · 2026-11-03 看过 · 32 KB`、`bar=35.4%`、`left=看到 0:43 / 共 2:00`、`more=本页最多显示 100 条，还有 5 条更早的记录未显示`；点 ✕ → `100 -> 99` 且 `location.hash` 不变；点首页标题 → `hash=#/history`、`histRows=100`、面包屑「▶ 播放记录」，首页卡片仍是 12 张。

### 🟡 齿轮与返回箭头分速隐藏

用户要求：齿轮 ⚙ 要跟浏览器自带控件条一起消失（更快），返回箭头 ← 保持 2.5 秒。

- CSS 拆成两条独立规则：`#back{transition:opacity .28s ease, transform .28s ease}` / `#tset{transition:opacity .16s ease, transform .16s ease}`，对应 `.vwrap.backhide #back` 与 `.vwrap.gearhide #tset`（齿轮过渡更短，淡出更干脆）
- JS 拆成双计时器：`CHROME_BACK_MS = 2500` / `CHROME_GEAR_MS = 1200`，`backTimer` + `gearTimer` 各自独立
- `backHide()` / `gearHide()` 各自判断 `!curItem || v.paused || v.ended` 与 `#tsheet` 是否展开，互不影响
- `chromeWake()` 的 200 ms 节流改为 `!backHidden && !gearHidden` 双条件，避免齿轮已经隐藏、返回还亮着时被节流吞掉唤醒

实测时序（`B`=返回箭头，`G`=齿轮，`1`=可见 `0`=隐藏）：`t0=B1G1  t600=B1G1  t1400=B1G0  t2000=B1G0  t2700=B0G0  wake=B1G1  paused=B1G1`——齿轮 1.2 秒先走、返回 2.5 秒后走，唤醒两者都回来，暂停常显。

### 🟡 版本号统一回 1.2.0

按用户要求不再每次改动都加版本号。`VERSION = "1.2.0"`（`video_server.py:53`）是唯一来源，页头 / 启动横幅 / `Server:` 头三处都由它派生；`README.md` 与 `REVIEW.md` 里的历史版本号也一并清掉，只保留「第 N 轮」这种叙述方式。

### 验证记录（第四轮）

| 项目 | 结果 |
| --- | --- |
| 版本一致性 | 页头 `LAN VIDEO · v1.2.0`、`Server: LANVideo/1.2.0 Python/3.15.0`、横幅 `已启动 · v1.2.0` |
| `thumb` 首次 | 4 个视频 → 新生成 4 / 已有 0 / 失败 0 |
| `thumb` 幂等 | 再跑一次 → 新生成 0 / 已有 4 |
| `thumb` 坏文件 | 111 个视频（含 2 个损坏）→ 新生成 6 / 已有 103 / 失败 2，坏文件无 jpg |
| 播放记录页 | `rows=100`、`sub=100 / 105 条`、进度条 35.4%、`more` 提示正确 |
| 标题入口 | 点首页「▶ 播放记录」→ `hash=#/history`、`histRows=100`、面包屑正确 |
| ✕ 单条移除 | `100 -> 99`，`location.hash` 保持不变 |
| 页头样式 | `header .hrow` 恢复为 1 个（类名撞车已修） |
| 分速隐藏 | `t1400=B1G0`、`t2700=B0G0`、唤醒 `B1G1`、暂停 `B1G1` |
| 旧标识残留 | `chromehide` / `chromeTimer` / `chromeHidden` / `prow1` / `practiceRow` / `paintPractice` / `上传视频到这里` / `✓ 管理` / `继续观看` 均为 0 处 |
| 语法 | `ast.parse` 通过；两个 `<script>` 块 `node --check` 通过 |

> 验证踩坑：headless Edge 的 `--dump-dom` 直接管道输出到 PowerShell 会得到 0 长度，必须 `cmd /c "... > file"` 重定向到文件再读；`--headless=new` 下 `--screenshot` 的相对路径不生效，必须给绝对路径。

---

## 三、未修改但值得关注

这些不属于 bug，改动收益与风险比不划算，或需要产品决策：

1. **单文件 4500 行**。`PAGE` 这个三引号字符串就占 2000 行，Python 后端和 HTML/CSS/JS 混在一起，没有语法高亮、没法 lint、diff 噪音大。若要长期维护，建议拆成 `server.py` + `web/index.html` + `web/app.js` + `web/style.css`，用 `importlib.resources` 或简单文件读取加载。但这会改变"单文件便携"的分发形态，属于权衡。

2. **同步阻塞的全盘扫描**。`search()`、`count_videos()`、`find_duplicates()`、控制台 `du` 都在请求线程里直接 `os.walk`。`ThreadingHTTPServer` 每请求一线程，大库（十万文件）时并发搜索会打满磁盘 IO 并堆积线程。可以加一个后台索引线程 + 内存倒排表，或至少给搜索加结果缓存与超时。

3. **转码并发固定 2 路**。`_TRANSCODE_SEM = Semaphore(2)` 在多人同时看不同格式时容易触发 503（30 秒 acquire 超时）。可以按 CPU 核心数/编码器类型动态设定（硬编可以放宽到 3–4，软编保持 1–2）。

4. **密码走明文 HTTP**。局域网内可嗅探。真要提升，需要自签证书 + HTTPS，但对"手机扫码即看"的体验是净损失，不建议默认开启。

5. **`_params()` 重复键取最后一个**。行为可接受，但 `parse_qs` 之后如果想支持多值（比如批量 `dir`），需要改成取 list。

6. **`progress.json` 每次全量读写**。实测 200 次 `progress_set` 约 492 ms（2.5 ms/次），单设备无感，但多设备高频上报时可加内存缓存 + 定时 flush。

7. **`is_removable_drive` 依赖 `ctypes.windll`**。非 Windows 平台恒返回 False（`try/except` 兜住），功能降级但不会崩。若要跨平台，需要按 `sys.platform` 分支。

8. **无自动化测试**。这次修复全靠手工 HTTP 复现验证。建议至少补一个 `tests/test_http.py`，用 `unittest` + `http.client` 覆盖 Range 解析、`resolve()` 路径校验、Cookie 校验、登录限速这几个纯逻辑点——这些恰好是最容易回归的部分。

---

## 四、验证记录

全部在隔离实例（`.testrun` + `.testmedia`，8899 端口，真实 ffmpeg 7.1 + NVENC）上完成，测试后已清理。

| 项目 | 结果 |
| --- | --- |
| 启动 | 横幅、`Server:` 头、页头三处版本号一致 |
| 端点冒烟（13 个） | 全部 200，无 500 |
| XSS | `next` 已转义为 `&quot;` |
| 安全头 | nosniff / SAMEORIGIN / same-origin / 完整 CSP 均存在 |
| Cookie 绕过 | 畸形 Cookie 全部 302 |
| 登录 | 正确 303 / 错误 401 / 第 8 次起 429（含正确密码） |
| CSRF | 跨站 Origin → 403；同源/无头 → 200 |
| Range（9 种） | 全部符合 RFC 7233 |
| 超大 body | 413 + 连接关闭，后续请求不再 414 |
| 路径穿越 | junction 逃逸 → 404 |
| 上传 | `.exe` → 400；`.mp4` → 200；库外目录 → 400 |
| 畸形 body（25 组） | 0 个 500 |
| 转码 | `old.avi` → 200 `video/mp4` |
| 无音轨视频 | `fast.mp4` → `direct`（原 `transcode_audio`） |
| fps | 29.97 / 23.976 / 25 全部正确 |
| HEAD `/video` | 响应头 519 字节，body 0 字节（未误发） |
| 真实库启动 | 4 个目录正常加载，`/api/list` 200 |

---

## 五、改动清单

四轮累计相对原始版本（`git diff --stat`）：

```
README.md        |   51 ++-
video_server.py  | 1148 +++++++++++++++++++++++++++++++++++++++++++++++---------
2 files changed, 1020 insertions(+), 179 deletions(-)
```

| | 行数 | 字节 |
| --- | --- | --- |
| 原始版本（`cbb714f`） | 4273 | 184 423 |
| 现在 `video_server.py` | 5079 | 222 780 |

原始版本可直接用 `git show cbb714f:video_server.py` 取出，与当前文件
`git diff cbb714f -- video_server.py` 逐行对照。
