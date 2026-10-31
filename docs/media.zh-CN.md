# 通用媒体中转协议

[English](media.md) · [中文版 README](../README.zh-CN.md)

## 上传与发布

上传只创建资产，不代表消息已发布。默认资产私有。multipart 中 **`targets` 必须先于 `file`**，服务端校验 Producer 身份及目标权限后才创建临时内容文件。`targets` 接受逗号分隔设备名或 JSON 数组。原文件名和客户端 MIME 不参与路径构造或格式判定。

```sh
curl https://mid.example.com/api/media \
  -H "Authorization: Bearer $PRODUCER_TOKEN" \
  -F 'targets=speaker,screen' -F 'file=@photo.jpg'
```

成功返回 HTTP 201，包含 `media_id`（随机 128 位十六进制 ID）、`mime_type`、`size_bytes`、`sha256`（小写十六进制）、`expires_at`（UTC Unix 秒）。JPEG/PNG 图片通过 Pillow 校验结构并解码，MP3 音频和 MP4 视频使用 ffprobe。音频只支持单个 MP3 音频流，可带内嵌 JPEG/PNG 封面，不接受 AAC/WAV/OGG。MP4 必须包含 H.264 视频，可包含 AAC 音频，不接受其他类型流；格式或编码不支持返回 415，缺少 ffprobe 返回 503。不做转码，也不保证所有终端硬件解码器都兼容。

同一个 Producer 通过 `/api/send` 引用资产，目标只能是上传时允许目标的子集，发布时重新校验权限和归属：

```sh
curl https://mid.example.com/api/send -H "Authorization: Bearer $PRODUCER_TOKEN" \
  -H 'Content-Type: application/json' -d '{
    "targets":["speaker","screen"],
    "media_id":"ASSET_ID",
    "event":{"title":"Photo","media":{"url":"mid://ASSET_ID"}}
  }'
```

`event` 保持既有终端格式；上述事件仅示意 URL 占位方式，并非完整严格 v2 终端消息。Mid 不修改事件字段，Hub 在转发终端前将对应的 `mid://ASSET_ID` 替换为自己的局域网缓存 URL。本版每次发布支持一个私有资产，可用 `media_id` 或单元素 `media_ids` 数组；支持同一资产多次发布和多个目标。不得同时提交两种 ID 字段，也不得混合私有 ID 和外部 URL。

响应包含 `delivery_ids`、`publication_id`、`delivered`、`buffered`。`delivered` 仅统计 WebSocket 写入成功，不代表已缓存；无论是否写入，待确认状态均持久保存。重新 POST `/api/send` 会创建新发布；Hub 使用 `delivery_id` 去重通知重放。Producer 应保存成功响应，避免网络结果不明时盲目重发造成新消息。

## Mid → Hub 外层描述

通过已认证设备 WebSocket 下发：

```json
{
  "type": "media.delivery",
  "delivery_id": "random-delivery-id",
  "publication_id": "random-publication-id",
  "event": {"media":{"url":"mid://ASSET_ID"}},
  "downloads": [{
    "media_id": "ASSET_ID",
    "url": "https://mid.example.com/api/media/ASSET_ID?ticket=SIGNED_JWT",
    "auth_mode": "DEVICE",
    "mime_type": "image/jpeg",
    "size_bytes": 12345,
    "sha256": "64-lowercase-hex-characters",
    "ticket_expires_at": 1793448300,
    "expires_at": 1793534400
  }]
}
```

`event` 面向终端；所有鉴权、传输元数据放在外层 `downloads`，不向终端严格 v2 `media` 对象注入字段。每台设备获得自己的签名 URL。`delivery_id` 标识持久投递，`publication_id` 标识发布。时间统一为 UTC Unix 秒。资产 `expires_at` 不可延长；票据 `ticket_expires_at` 可在重放或刷新时更新。

### 下载、Range 和票据刷新

```sh
curl "$DOWNLOAD_URL" -H "Authorization: Bearer $DEVICE_TOKEN" -o content.part
curl "$DOWNLOAD_URL" -H "Authorization: Bearer $DEVICE_TOKEN" -H 'Range: bytes=1048576-' -o tail.part
curl -X POST "https://mid.example.com/api/media/$MEDIA_ID/ticket" \
  -H "Authorization: Bearer $DEVICE_TOKEN"
```

刷新接口向仍有目标权限的合法 Device 返回新下载描述，资产期限不变。重连自动重放所有未完成的私有媒体投递，并生成新票据。在线 Hub 遇到中断或票据失效，可刷新后重新下载，不需要 Producer 重新发布。

票据在期限内允许重复 GET 和单段 Range，不会首次使用即作废。支持 `bytes=start-end`、`bytes=start-` 和 `bytes=-suffix`；无效或多段 Range 返回 416 及 `Content-Range: bytes */SIZE`。响应提供 `Accept-Ranges`、`Content-Length`、SHA-256 `ETag`；206 另含 `Content-Range`。`If-Range` 不匹配时返回完整文件。对拼合后的完整文件校验大小及哈希，不能仅校验某个 Range。

下载同时要求有效 Device Token，以及绑定资产、设备、用途 `media.download`、签发方 `pinepi-broadcast-mid`、受众 `pinepi-hub-media` 的有效 HS256 JWT。服务端固定仅允许 HS256。只有票据或只有 Token 都不能下载。刷新需要 Device Token 和当前目标权限。知道其他 Producer 的资产 ID 不授予任何权利；没有公开下载接口或 Producer 删除接口。

### 缓存完成确认

Hub 必须完成全部下载、大小及 SHA-256 校验、原子持久保存缓存文件，并且**已持久保存消息和媒体引用**后，才发送：

```json
{"type":"media.cached","media_id":"ASSET_ID","delivery_id":"DELIVERY_ID","sha256":"COMPLETE_FILE_SHA256"}
```

成功响应 `{"type":"media.cached.result","status":"cached",...}`；失败响应含 `status:error`、HTTP 风格 `code`、`detail`。等价 HTTP 接口：

```sh
curl "https://mid.example.com/api/media/$MEDIA_ID/cached" \
  -H "Authorization: Bearer $DEVICE_TOKEN" -H 'Content-Type: application/json' \
  -d '{"delivery_id":"DELIVERY_ID","sha256":"COMPLETE_FILE_SHA256"}'
```

设备身份来自认证连接或 Token，不能通过请求中的 `device` 字段指定其他设备。确认校验对应投递目标和完整哈希。重复确认成功；资产完成后已删除内容时，只要墓碑元数据尚在，重复确认仍成功。终端原有“消息已保存”ACK、WebSocket 写入成功、HTTP/Range 响应结束都不影响媒体确认或保留状态。

## 外部 URL：`NONE`

```json
{
  "targets": ["speaker"],
  "media_urls": ["https://cdn.example.org/video.mp4?external_signature=..."],
  "event": {"media":{"url":"https://cdn.example.org/video.mp4?external_signature=..."}}
}
```

`/api/send` 生成外层 `downloads:[{"url":"...","auth_mode":"NONE"}]`，采用普通消息投递与内存离线缓冲。不创建托管资产，也不删除外部源文件。客户端不能直接提供外层 `downloads` 或 `auth_mode`；Mid 域名和 `/api/media` 路径不能作为外部 URL 提交。修改模式不会绕过私有下载接口的权限检查。不得根据 URL 是否含有 `token` 判断模式。

Mid 不抓取外部 URL，只在发布时拒绝非 HTTPS、用户密码信息、非默认端口、明显私网 IP、本地域名和 Mid 资产地址。**Hub 下载端必须独立实施网络边界校验**：DEVICE 只向配置的受信任 Mid HTTPS 源发送凭证；重定向不得继承 Authorization，建议直接拒绝 DEVICE 重定向。NONE 即便包含外部签名参数，也不添加 Mid 凭证。

每个外部地址及每次重定向都需要解析、验证目标 IP，拒绝环回、私有、链路本地、保留和云元数据地址，包含 IPv6 和映射 IPv4。实际连接绑定已验证 IP，防止 DNS 重绑定；检查 TLS 主机名，限制跳转次数、时间、下载字节数。发布时的域名初筛不能代替 Hub 下载时的校验。

## 无数据库生命周期

```text
/var/lib/pinepi-broadcast-mid/media/
  .lock
  rates.json
  .upload-RANDOM
  RANDOM_MEDIA_ID/
    content
    meta.json
```

元数据保存可读的 `producer_id`、由该稳定 ID 计算的 `owner` SHA-256、允许目标、MIME、实际大小与哈希、创建及到期时间、状态、发布事件、**全部投递引用**以及各目标 `pending` / `cached` / `expired` 状态。不持久化明文 Token 或下载票据。JSON 使用临时写入、fsync 和原子替换；内容先 fsync、改名，再提交元数据。失败上传、废弃临时文件和孤立目录会清理。上传频率状态也持久化。元数据损坏时拒绝启动，避免静默丢弃未完成投递；备份时停机复制整个数据目录和配置。

进程内锁协调元数据更新、下载打开和清理，生命周期 flock 确保单进程。最多同时接受一个上传，其余返回 429，每次上传有时限。实际读取字节、头、目标字段、文件数量、格式、Producer 容量和总磁盘均受限制。服务容量统计包含元数据、墓碑和孤立文件，Producer 容量统计仍保留的内容文件；磁盘需要额外余量供原子元数据替换。数量限制约束元数据增长。JSON 单独限流式实际字节；未认证请求在读取或解析请求体前拒绝。

全部发布引用的全部目标确认缓存后，经过宽限期删除内容。宽限期内新增发布会重置完成状态。上传后未发布的资产保留到固定最大期限；到期的未完成投递标记 `expired`，不能计为成功。已经打开的下载会保留文件直到最后一个读者关闭；逻辑删除或到期后，不再允许新下载或刷新。内容删除后墓碑保留七天，再删除元数据。启动及定时任务执行清理；重启从文件恢复，重连重发未完成通知。Mid 删除内容不应导致 Hub 缓存删除。

状态码：401 凭证错误或身份类型错误；403 票据无效/过期/绑定不匹配或 Producer 无目标权限；404 不存在或不可访问的资产/投递；409 确认哈希不匹配；410 可访问资产已到期或删除；413 超大小；415 格式/编码无效；422 字段、顺序、目标或 URL 不合法；429 上传频率、Producer 容量、并发上传或交付数量限制；507 服务磁盘/资产数量限制；408 上传超时；503 媒体功能禁用或校验器不可用。

## 配置与默认值

以下均位于 `media`。`signing_key_base64` **无默认值**，必须提供至少 32 字节独立随机密钥的标准 Base64。`public_base_url` 是受信任的公开 HTTPS 源，默认示例为 `https://mid.example.com`，不会使用请求 Host 推导。更换密钥会使现有票据失效，合法 Hub 可刷新。

| 配置项 | 默认值 |
|---|---:|
| `data_dir` | `/var/lib/pinepi-broadcast-mid/media` |
| `max_image_pixels` | 16000000（1600 万像素） |
| `max_upload_bytes` | 104857600（100 MiB） |
| `producer_capacity_bytes` | 524288000（500 MiB） |
| `total_capacity_bytes` | 5368709120（5 GiB） |
| `uploads_per_minute` | 10（固定 60 秒窗口，计入失败尝试） |
| `retention_seconds` | 86400（上传后 24 小时） |
| `ticket_seconds` | 300（5 分钟，不超过资产期限） |
| `grace_seconds` | 60 |
| `cleanup_interval_seconds` | 30 |
| `upload_timeout_seconds` | 120 |
| `temporary_retention_seconds` | 600 |
| `tombstone_seconds` | 604800（7 天） |
| `max_assets` | 10000（包含墓碑） |
| `max_deliveries_per_asset` | 200 |

数字配置必须为正整数。multipart 请求额外允许 16 KiB，目标字段和 multipart 头各最多 4 KiB。JSON 请求 64 KiB；墨水屏图片 1 MiB 加请求开销。Producer 资产归属和持久上传配额始终使用 `producers` 的稳定键名，不使用 Token 或说明；轮换 Token 时保持键名不变。详见[身份结构](../README.zh-CN.md#身份与权限)，旧的 Token 键名结构会被拒绝。重启后移除设备会使其旧 Token 失效，已有待确认引用仍在原期限到期。

## systemd 与 Nginx

首次安装、升级和环境修复统一执行 `sudo bash install.sh`，保留生产配置，先准备并校验新版本和虚拟环境，再切换服务和网页，启动检查失败自动回退。详见[安装流程](../README.zh-CN.md#安装)。服务通过 `/opt/pinepi-broadcast-mid/current/venv/bin/python -m uvicorn` 运行，配置提供 `StateDirectory=pinepi-broadcast-mid`、`StateDirectoryMode=0700`、`UMask=0077` 及可写的 `/var/lib/pinepi-broadcast-mid`。配置目录对应用保持只读；移动数据目录时需同步修改属主和 `ReadWritePaths`。安装时同时复制 `media_store.py` 并安装 FFmpeg。公网必须使用 HTTPS/WSS。

Nginx 的 TLS 虚拟主机参考配置如下，保留已有证书设置：

```nginx
# http 上下文：不要记录 $request、$request_uri、查询参数或 Authorization
log_format mid_safe '$remote_addr $request_method $uri $status $body_bytes_sent';

# server 上下文
access_log /var/log/nginx/mid-access.log mid_safe;
location = /api/media {
    # 错误日志可能包含原始签名 URL；禁用或使用经过验证的脱敏日志管线
    error_log /dev/null;
    client_max_body_size 101m;
    client_body_timeout 120s;
    proxy_request_buffering off;
    proxy_http_version 1.1;
    proxy_pass http://127.0.0.1:8765;
    proxy_read_timeout 150s;
}
location ^~ /api/media/ {
    error_log /dev/null;
    client_max_body_size 64k;
    proxy_pass http://127.0.0.1:8765;
    proxy_buffering off;
    proxy_read_timeout 150s;
}
location = /api/send-image-2-13inch-touch-e-paper-hat-with-case/ {
    client_max_body_size 2m;
    proxy_request_buffering off;
    proxy_pass http://127.0.0.1:8765;
}
location /api/ {
    client_max_body_size 64k;
    proxy_request_buffering off;
    proxy_pass http://127.0.0.1:8765;
}
location /ws/ {
    proxy_pass http://127.0.0.1:8765;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 86400;
}
```

修改应用限额时同步调整代理限额。关闭上传代理缓冲，避免应用拒绝前代理已缓存未授权内容。应用对 Uvicorn 访问日志的 `ticket` 参数脱敏且不记录 Authorization；负载均衡、APM、HTTP 客户端和调试日志也需遵循此规则。外部签名 URL 也可能包含秘密。不要公开暴露上游端口。

## Hub 项目配套工作

配套 Hub 私有缓存已实现 DEVICE 受信任源校验、禁止重定向、下载重试与刷新票据、完整大小/哈希校验、原子缓存、消息引用持久化、完成确认和局域网 URL 改写，重连或重启后复用已校验缓存。托管内容默认最多 512 MiB、10000 个文件，容量不足时拒绝新下载，不删除无法证明已无历史引用的文件；按历史引用自动回收仍为独立工作。NONE URL 不会附带 Mid 凭证下载。完成确认是合法 Hub 的承诺，不是 JWT 提供的落盘证明。应使用真实 Hub 验证 Mid 清理后终端仍可打开历史媒体，Mid 单仓库测试不能证明这一端到端条件。网页/YouTube 解析、抓取和通用转码不在本期范围。

实现参考：[PyJWT 验证 API](https://pyjwt.readthedocs.io/en/latest/api.html) 与 [python-multipart 流式回调](https://multipart.fastapiexpert.com/api/)。

网页示例为 IMAGE/AUDIO/VIDEO 提供本地文件上传，不为 TTS 提供音频 URL 输入框；TEXT/TTS 只转发正文，由 Hub 合成，媒体上传不会触发合成。NORMAL/CRITICAL 是所有消息类型共有的独立严重程度。
