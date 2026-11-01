# 第三方组件许可声明（Third-Party Notices）

本项目（局域网视频站 / lan-video-site）的站点代码 `video_server.py` 为个人作品。
便携分发包中捆绑了以下第三方组件，分发时必须保留本文件：

## 1. Python 运行时（便携包 `python/` 目录）

- 版本：Python 3.13.x（Windows 嵌入式发行版 embeddable package）
- 许可证：**PSF License Version 2**（Python Software Foundation License）
- 允许商用与再分发，无需开放你自己的代码；但需保留本声明与 PSF 许可证文本。
- 许可证全文：<https://docs.python.org/3/license.html>
- 源码：<https://www.python.org/source/>

## 2. FFmpeg（便携包根目录 `ffmpeg.exe`）

- 版本：FFmpeg 7.1（gyan.dev "essentials" 构建，`--enable-gpl --enable-version3`）
- 许可证：**GNU General Public License version 3 (GPL-3.0)**
  —— 因该构建启用了 `--enable-gpl`（含 libx264 / libx265 等组件），整个二进制按 GPL-3.0 授权。
- 分发义务（GPL-3.0）：分发二进制时需
  1. 附上 GPL-3.0 许可证文本：<https://www.gnu.org/licenses/gpl-3.0.txt>；
  2. 提供对应源码或书面源码获取途径（源码链接即可，见下）。
- 源码：<https://ffmpeg.org/releases/ffmpeg-7.1.tar.xz>
  （构建配置说明：<https://www.gyan.dev/ffmpeg/builds/>）
- FFmpeg 项目本身：<https://ffmpeg.org> —— 包含众多组件，各自许可见源码内的 `COPYING.GPLv3` 等文件。

### 不想被 GPL 约束？

GPL 只在「分发」时生效：自己用、局域网内给家人用，都没有任何义务。
若要公开发布且完全避开 GPL，可改用 **LGPL 构建**的 ffmpeg（如 BtbN 的
`ffmpeg-master-latest-win64-lgpl.zip`，<https://github.com/BtbN/FFmpeg-Builds/releases>），
但 LGPL 构建不含 libx264（CPU 软编回退不可用，GPU 编码 NVENC/QSV/AMF 不受影响）。

## 3. imageio-ffmpeg（仅当用 `python video_server.py` 方式运行且系统装了该库时）

- pip 包名：imageio-ffmpeg，许可：BSD-2-Clause
- 本项目的代码只在其存在时调用它查找 ffmpeg 二进制；便携包不包含该库。
- <https://github.com/imageio/imageio-ffmpeg>

---

> 简单结论：**自己用 = 无任何要求；上传 GitHub 分发 = 保留本文件并附 GPL-3.0 文本即可。**
