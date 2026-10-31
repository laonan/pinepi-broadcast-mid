# PinePi Broadcast Mid

[English](README.md) · [媒体协议](docs/media.zh-CN.md) · [Media protocol](docs/media.md)

基于 FastAPI 的消息与私有媒体中转服务。Producer 按设备权限发布消息，也可上传 JPEG、PNG、H.264/AAC MP4。Mid 负责临时交付，Hub 独立保存终端历史消息引用的缓存。现有文本广播和独立墨水屏转换接口继续提供。

## 安装

要求 Linux、Python 3.11–3.13、FFmpeg（含 ffprobe）。

```sh
git clone https://github.com/laonan/pinepi-broadcast-mid.git
cd pinepi-broadcast-mid
cp config.json.example config.json
# 先填写凭证、media.public_base_url 和 media.signing_key_base64
python3 -c 'import secrets; print(secrets.token_urlsafe(32))' # 为每个凭证分别执行
python3 -c 'import base64,secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())' # 独立签名密钥
sudo bash install.sh
```

安装器创建独立服务用户，安装 Python 依赖及 FFmpeg，并启动 systemd 服务。配置位于 `/etc/pinepi-broadcast-mid/config.json`（0600）；持久数据位于 `/var/lib/pinepi-broadcast-mid/`（0700）。反向代理负责 HTTPS/WSS。必须运行**单个 Uvicorn worker**，不要启用 reload；文件锁会拒绝第二个进程访问同一媒体目录。

手动启动：

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
PINEPI_BROADCAST_CONFIG="$PWD/config.json" .venv/bin/uvicorn broadcast_server_mid:app \
  --app-dir python --host 127.0.0.1 --port 8765 --workers 1 --ws-max-size 65536
```

本地开发需要把 `media.data_dir` 改为可写路径；生成的下载 URL 仍使用配置的 HTTPS 域名。删除整个 `media` 配置可禁用媒体接口，继续使用文本和墨水屏接口；媒体配置无效会阻止启动。

## 身份与权限

`producers` 以 Producer Token 为键，值为 `{"targets":"*"}` 或 `{"targets":["speaker"]}`；`devices` 以设备名为键，值为 `{"token":"DEVICE_TOKEN"}`。**各设备的 Token 必须唯一，且不能与 Producer Token 共用**；沿用旧示例共用凭证的部署需要先迁移。Producer 可配置唯一且稳定的 `id`，用于轮换 Token 后保留资产归属；没有 `id` 时使用 Token 的 SHA-256。不同 Producer 不得共用 `id`。

HTTP 使用 `Authorization: Bearer …`。Producer 只能上传和发布；Device 负责连接、下载、刷新票据和确认缓存。独立随机签名密钥至少 32 字节，只在 Mid 保存，用于 HS256 票据签名，不能代替上传凭证。配置变更后重启服务。

## 原有接口

JSON 请求上限 64 KiB，每 Producer 每分钟最多 100 次发布：

```sh
curl https://mid.example.com/api/send -H "Authorization: Bearer $PRODUCER_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"targets":["speaker"],"event":{"title":"Hello","content":"World"}}'
```

`targets:["*"]` 选择 Producer 有权限的已配置设备。没有 `event` 的旧请求仍包装为 `{"event": body}`。普通文本离线缓冲保存在内存，每设备最多 200 条、2 MiB；私有媒体投递会持久化。

设备连接 `wss://mid.example.com/ws/` 后，五秒内发送 `{"device":"speaker","token":"DEVICE_TOKEN"}`。收到 `{"type":"ping"}` 后回复 `{"type":"pong"}`。新连接替换同设备旧连接。另见 [Python 订阅示例](examples/subscribe_demo.py) 和 [网页演示](examples/web/index.html)。

独立墨水屏接口仍接受 Pillow 支持的常见图片，文件最大 1 MiB：

```sh
curl https://mid.example.com/api/send-image-2-13inch-touch-e-paper-hat-with-case/ \
  -H "Authorization: Bearer $PRODUCER_TOKEN" -F 'targets=epaper' -F 'image=@photo.png'
```

在线设备收到 4000 字节原始二进制 WebSocket 帧（250×122 单色）；不生成通用媒体资产，也不缓冲离线二进制帧。

## 媒体、部署与验证

[完整媒体协议](docs/media.zh-CN.md)包含上传、发布、票据刷新、鉴权下载、Range、完成确认、全部配置默认值和 Nginx 配置。无需 SQLite、Redis 或其他数据库；不抓取外部 URL，不解析网页，不做通用转码。Hub 缓存与终端 URL 改写由 Hub 项目另行实现。

```sh
sudo systemctl restart pinepi-broadcast-mid
journalctl -u pinepi-broadcast-mid -f
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

测试需要 ffmpeg/ffprobe，覆盖安全校验、投递持久化、重试、Range、清理和原有接口兼容性。MIT License。
