# Pero-bilibili · B 站视频理解插件

让 [Pero](https://github.com/3523509051/Pero) 理解你分享的 B 站视频。发送视频链接后，插件读取视频信息、可用字幕与故事板，生成带真实时间戳的分镜图，再交给 Agent 理解内容。

**v0.1.0 · 首个公开试验版** · Pero Plugin SDK v1 RC · MIT License

## 功能

- 支持电脑端、手机端标准 BV 视频链接、分享参数及 `b23.tv` 短链接。
- 从公开视频的故事板提取实际时间点的画面，最多六帧合成一张图，无需默认下载整段视频。
- 读取可获取的字幕，保存完整时间戳字幕文件，向 Agent 返回有限摘录及分镜图。
- 按 BV、CID、分 P 及处理配置缓存，重复访问时复用已有拼图与字幕，减少重复网络读取。
- 后台执行：视频处理期间正常聊天，完成后由 Pero 回报结果；也可从控制站查看或取消任务。

## 安装到 Pero

需要支持后台工具与受控短链读取的 **Pero Plugin SDK v1 RC**。

1. 打开 Pero 控制站 → **插件** → **从 GitHub 固定版本安装**。
2. 输入以下仓库地址，版本填写 `v0.1.0`。

   ```text
   https://github.com/3523509051/Pero-bilibili
   ```

3. 预览插件、提交版本、清单和所需权限，确认安装。安装后默认停用。
4. 在插件详情中批准需要的公开网络域名；启用插件，并在指定群聊或私聊中使用。
5. 发送一条视频链接，例如：

   > 帮我看看这个视频主要讲了什么：https://www.bilibili.com/video/BV1xx411c7mD

插件注册的唯一工具为 `watch_bilibili(url, refresh=false)`。如需绕过已有缓存重新处理，可用 `refresh=true` 发起新请求；普通重复访问优先命中缓存。

## 链接支持

| 类型 | 示例 | 当前支持 |
|---|---|---|
| 标准 BV | `https://www.bilibili.com/video/BV1xx411c7mD` | 是 |
| 分享参数 | 标准 BV 链接附带 `?spm_id_from=...` | 是 |
| 手机端 BV | `https://m.bilibili.com/video/BV1xx411c7mD` | 是 |
| 短链接 | `https://b23.tv/…` | 通过受控跳转解析，最多三次；取决于服务端网络及域名许可 |
| 分 P | `...?p=2` | 元数据及可用字幕按目标 CID 读取；画面仅支持第 1P |
| AV、番剧、直播、动态 | — | 暂不支持 |

如果短链接或某个资源域名无法读取，请先尝试完整 BV 链接。不绕过网络限制，也不把第一 P 的画面用于其他分 P。

## 配置与权限

| 配置 | 默认值 | 说明 |
|---|---|---|
| `frame_count` | 6 | 采样帧数，1–6 |
| `subtitle_language` | `auto` | 自动选择或 `zh-CN`、`en-US` |

需按实际功能批准的公开 HTTPS 域名：

| 域名 | 用途 |
|---|---|
| `api.bilibili.com` | 视频信息、故事板索引、字幕列表 |
| `i0.hdslb.com` | 已核对的故事板图片服务 |
| `b23.tv` | 短链接入口 |
| `www.bilibili.com` | 短链接的标准视频目标 |
| `m.bilibili.com` | 手机端视频目标 |

插件在隔离 worker 内运行，通过 SDK 读取公开服务、独立缓存和发布图片。它不需要登录 B 站，不保存 Cookie，也不点赞、评论、关注或发私信。字幕 CDN 若使用未批准的新域，可能只返回部分结果；需要补充有证据的域名声明后重新安装相应版本。

## 独立业务模块

`bilibili_video/` 可以脱离 Pero 直接调用。Python 3.10+，安装固定依赖：

```bash
python -m pip install -r requirements.lock.txt
```

```python
import asyncio
from pathlib import Path
from bilibili_video import VideoOptions, inspect_video

async def main():
    result = await inspect_video(
        "https://www.bilibili.com/video/BV1xx411c7mD",
        VideoOptions(frame_count=6),
        Path("video-cache"),
    )
    print(result.public())

asyncio.run(main())
```

独立调用使用自己的只读 HTTP fetcher；在 Pero 运行时改用受控 SDK fetcher。二者共享视频解析、画面拼合、字幕和缓存业务逻辑。独立用法不需要 Agent 框架或模型 API Key。

## 实现与验证状态

业务层按故事板真实 `index` / `pvdata` 时间戳取帧，Pillow 解码 JPEG/PNG；完整字幕及图像保存在独立缓存，返回值不包含完整视频或 Base64。后台任务的运行、取消、图片输入与结果回报由宿主负责。

已完成公开视频元数据和 JPEG 故事板的独立读取核对，以及 SDK 适配、短链、缓存和产物的离线核对。首个 GitHub 导入及实际聊天回报仍属试验验证；网络、字幕可用性和模型的图片能力会影响最终结果。

## 许可证与参考

[MIT](LICENSE)。参见 [NOTICE](NOTICE.md)。设计参考了 [dsh-bili-scroll](https://github.com/c1overzz/dsh-bili-scroll) 的公开视频故事板思路，本项目无需 DSH 运行时。
