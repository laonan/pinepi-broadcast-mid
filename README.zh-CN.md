# PinePi Broadcast Mid

[English](README.md) · [媒体协议](docs/media.zh-CN.md) · [Media protocol](docs/media.md)

基于 FastAPI 的消息与私有媒体中转服务。Producer 按设备权限发布消息，也可上传 JPEG、PNG、MP3 音频和 H.264/AAC MP4。Mid 负责临时交付，Hub 独立保存终端历史消息引用的缓存。现有文本广播和独立墨水屏转换接口继续提供。

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

**首次安装、升级和修复都使用同一个安装脚本**：

```sh
cd /home/ubuntu/pinepi-broadcast-mid # 按实际源码目录调整
git pull --ff-only
sudo bash install.sh
```

如果 `/etc/pinepi-broadcast-mid/config.json` 已存在，脚本始终保留，即使源码目录里有其他 `config.json` 也不会覆盖。首次安装才读取源码目录中的配置；两处都没有时会创建受保护的模板并退出，填写后重新执行同一命令。不再提供“带占位凭证继续启动”的选项。已有凭证会校验，不会自动轮换或合并；旧配置没有 `media` 时保持媒体功能禁用，启用上传需要显式补充该节。

每次执行都会将全部应用模块和网页资源准备到新的 `/opt/pinepi-broadcast-mid/releases/<release>/` 永久目录，并在其中创建**全新虚拟环境**，因此也能修复旧环境缺失或损坏的依赖。安装依赖、`pip check`、依赖导入和应用配置启动校验都在停止旧服务之前完成。脚本选择 Python 3.11–3.13，安装 FFmpeg，并在需要时安装对应的 venv 包，再以服务用户身份校验配置。找不到兼容 Python 时，会明确退出；使用发行版软件包安装后，可执行 `sudo bash install.sh --python /usr/bin/python3.12`，指定实际安装的版本。

准备成功后，脚本切换 `/opt/pinepi-broadcast-mid/current`，安装并重新加载、启用 systemd unit，重启服务，等待本地无凭证 `POST /api/send` 返回 **401**。启动或就绪检查失败时恢复原 unit、版本链接及网页；原先正常运行的服务会重新启动。回滚无法修好部署前本就损坏的旧服务。切换前失败不会替换旧运行版本。准备好的版本和旧版本保留以便排查，只在不再需要回退时清理非活动版本。安装锁防止并发执行。重启仍会丢弃普通文本的内存离线缓冲；持久媒体投递从独立数据目录恢复。

网页自动部署到 `/var/www/pinepi-broadcast-mid`，该路径链接至活动版本的网页专用目录，原目录或链接另存为旁边的 `.before-<release>`。若已有 Nginx 的**独立站点**静态目录，每次升级可用 `sudo bash install.sh --web-dir /现有站点静态目录`；不要指定与其他网站共用的目录。脚本不修改域名、TLS 证书及 Nginx 配置，现有 root/alias 指向该目录即可，或在 HTTPS server 中加入：

```nginx
location /demo/ {
    alias /var/www/pinepi-broadcast-mid/;
    index index.html;
}
```

保留[部署说明](docs/media.zh-CN.md#systemd-与-nginx)中的 API、WebSocket、媒体代理规则，代理变更后执行 `sudo nginx -t` 和 `sudo systemctl reload nginx`。页面地址为 `https://你的Mid域名/demo/`，连接设置填写 `https://你的Mid域名/api/send` 和 Producer Token。

配置仍位于 `/etc/pinepi-broadcast-mid/config.json`（0600），持久数据仍位于 `/var/lib/pinepi-broadcast-mid/`（0700），部署不会替换媒体数据。已有直接安装到 `/opt/pinepi-broadcast-mid` 的旧版也使用同一脚本迁移，旧文件和旧虚拟环境会保留。新 unit 通过 `current/venv/bin/python -m uvicorn` 运行**单个 worker**，文件锁拒绝并发媒体进程。生产代码保留在 `/opt`；service 的 `ProtectHome=true` 会阻止直接从 `/home` 源码目录运行。

本地开发可手动启动：

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
PINEPI_BROADCAST_CONFIG="$PWD/config.json" .venv/bin/uvicorn broadcast_server_mid:app \
  --app-dir python --host 127.0.0.1 --port 8765 --workers 1 --ws-max-size 65536
```

本地开发需要把 `media.data_dir` 改为可写路径；生成的下载 URL 仍使用配置的 HTTPS 域名。删除整个 `media` 配置可禁用媒体接口，继续使用文本和墨水屏接口；媒体配置无效会阻止启动。

## 身份与权限

两个配置节都使用稳定、可读的 ID 作为键。Producer 代表发送消息的应用或集成，Device 代表接收消息的 Hub 或设备适配器。身份配置示例（保留原有独立 `media` 配置）：

```json
{
  "producers": {
    "web-console": {
      "description": "手动发送消息的网页控制台",
      "token": "REPLACE_WITH_PRODUCER_WEB_TOKEN",
      "targets": "*"
    },
    "morning-news": {
      "description": "每天推送早间新闻的自动化程序",
      "token": "REPLACE_WITH_PRODUCER_NEWS_TOKEN",
      "targets": ["speaker"]
    }
  },
  "devices": {
    "speaker": {
      "description": "客厅 Hub",
      "token": "REPLACE_WITH_DEVICE_SPEAKER_TOKEN"
    },
    "epaper": {
      "description": "厨房墨水屏适配器",
      "token": "REPLACE_WITH_DEVICE_EPAPER_TOKEN"
    }
  }
}
```

- 键名（如 `web-console`、`speaker`）就是稳定身份 ID，最多 128 个字符，不能含空白或控制字符。Producer ID 决定资产归属和配额身份；Device ID 用于 `targets`、WebSocket 的 `device` 及票据绑定，创建后应保持稳定。
- `token` 必填，为非空、无空白的可打印 ASCII 凭证。各 Producer 和 Device 的 Token 必须全局唯一，不能跨身份类型共用。ID 不代替凭证，HTTP Bearer 中填写 `token` 的值。
- `description` 为可选用途说明，最多 500 个字符，省略时为空。修改说明不改变身份、权限或媒体归属，不参与鉴权，也不会下发给终端。
- Producer 的 `targets` 必填：`"*"` 表示全部已配置设备，数组填写实际 Device ID，空数组表示没有目标权限。Device 不获得上传或发布权限。

**不支持以 Token 为键的旧 Producer 结构，也不支持条目内旧的 `id` 覆盖字段。** 未知身份字段、重复 JSON 键、重复凭证和不存在的权限目标都会导致校验失败。升级前，请按新示例调整 `/etc/pinepi-broadcast-mid/config.json` 中的身份配置；保持 Device ID 和原本已分离的 Token 不变即可继续使用现有客户端。安装器保留生产配置，不自动转换或覆盖，并会在下载依赖前先检查身份结构，再在切换服务前完成应用启动校验。

同一个 `producers.<id>` 下更换 `token`，资产归属、内容容量统计和持久上传频率状态保持不变。普通 JSON 发布频率也按 Producer ID 计数，但保存在内存，进程重启时重置。更换键名则视为新的 Producer。新元数据记录可读的 `producer_id`，`owner` 为该 ID 的哈希，不回退使用凭证哈希识别身份。旧资产若按 Token 计算归属，不会自动转移；若旧配置已使用显式稳定 `id`，将其作为新键名可以保持相同归属哈希。

HTTP 使用 `Authorization: Bearer …`。Producer 只能上传和发布；Device 负责连接、下载、刷新票据和确认缓存。独立随机签名密钥至少 32 字节，只在 Mid 保存，用于 HS256 票据签名，不能代替上传凭证。配置变更后重启服务。

## 原有接口

JSON 请求上限 64 KiB，每 Producer 每分钟最多 100 次发布：

```sh
curl https://mid.example.com/api/send -H "Authorization: Bearer $PRODUCER_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"targets":["speaker"],"event":{"title":"Hello","content":"World"}}'
```

`targets:["*"]` 选择 Producer 有权限的已配置设备。没有 `event` 的旧请求仍包装为 `{"event": body}`。普通文本离线缓冲保存在内存，每设备最多 200 条、2 MiB；私有媒体投递会持久化。

[网页示例](examples/web/index.html)提供四种消息类型：TEXT、IMAGE、AUDIO、VIDEO。**严重程度独立于类型**，NORMAL / CRITICAL 适用于任意一种消息（需 Hub/终端 0.8.4+），不是第五种类型，切换示例时也会保留严重程度。文本可选 PRIVATE / TTS；TTS 完全由 Hub 合成，网页只提交正文、不带 `speech`，不上传文本音频，也不提供朗读 URL 输入框，Mid 仅转发请求。

图片、音频和视频选择本地 JPEG/PNG（最多 8 MiB）、MP3（16 MiB）或 H.264/AAC MP4（100 MiB）。可先点击 Upload file 创建资产，或直接 Upload & send 自动先上传再发布。表单按 targets 在前、file 在后的顺序上传，使用服务端校验返回的 MIME、大小、哈希和资产 ID，通过 `media_id` 发布，不再预填不存在的媒体 URL。上传提供进行中提示和取消按钮。更换文件、类型、目标或连接后需重新上传，修改严重程度或标题可复用资产；发布失败时，只要资产仍可用，就可重用已完成上传。历史仅保存资产元数据，不保存文件字节、票据或 Device Token，媒体不可用时需要重新上传。

配套 Hub 需要包含 Mid 私有媒体缓存功能，并使用原有 `--media-directory`、`--media-base-url` 参数。Hub 使用自己的 Device Token 下载，校验大小/哈希，原子缓存，持久保存改写为局域网 URL 的消息后，才发送 `media.cached`；Mid 清理不删除 Hub 副本。上传媒体缓存默认 512 MiB，可用 `--media-cache-bytes` 调整；容量不足会拒绝新下载，不自动删除历史可能引用的文件。音视频主体仍由用户手动播放，独立墨水屏裁剪功能保留。重试保持 ID 并会去重，编辑消息或点击 New ID 可创建新消息；两个目标输入框继续提供历史设备快捷按钮。


建议通过 Nginx 将静态页面托管在 Mid 同源，或由代理配置明确可信来源的 CORS；独立域名页面发送 Bearer 请求需要跨域许可。连接设置及近期消息仍保存在浏览器 localStorage。前端协议测试：`node --test tests/test_web_protocol.cjs`（Node 18+）。

设备连接 `wss://mid.example.com/ws/` 后，五秒内发送 `{"device":"speaker","token":"DEVICE_TOKEN"}`。收到 `{"type":"ping"}` 后回复 `{"type":"pong"}`。新连接替换同设备旧连接。另见 [Python 订阅示例](examples/subscribe_demo.py) 和 [网页演示](examples/web/index.html)。

独立墨水屏接口仍接受 Pillow 支持的常见图片，文件最大 1 MiB：

```sh
curl https://mid.example.com/api/send-image-2-13inch-touch-e-paper-hat-with-case/ \
  -H "Authorization: Bearer $PRODUCER_TOKEN" -F 'targets=epaper' -F 'image=@photo.png'
```

在线设备收到 4000 字节原始二进制 WebSocket 帧（250×122 单色）；不生成通用媒体资产，也不缓冲离线二进制帧。

## 媒体、部署与验证

[完整媒体协议](docs/media.zh-CN.md)包含上传、发布、票据刷新、鉴权下载、Range、完成确认、全部配置默认值和 Nginx 配置。无需 SQLite、Redis 或其他数据库；不抓取外部 URL，不解析网页，不做通用转码。私有媒体缓存与终端 URL 改写位于配套 Hub 项目，部署时需要同步升级该项目。

```sh
sudo systemctl restart pinepi-broadcast-mid
journalctl -u pinepi-broadcast-mid -f
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

测试需要 ffmpeg/ffprobe，覆盖安全校验、投递持久化、重试、Range、清理和原有接口兼容性。MIT License。

运行双仓库上传、缓存确认和 Mid 清理集成检查时，设置 `PINEPI_HUB_SOURCE=/实际路径/pinepi-thinksmart-view/hub/src`，再执行 `pytest tests/test_media.py`；测试使用本地 ASGI 传输和虚拟凭证，不调用外部供应商。
