# NOTICE

`bilibili_video/` 由 Pero 项目按《B站视频理解模块-实现规范》独立编写，只使用 Python 标准库、httpx 与 Pillow；未复制 DSH、bilibili-mcp 或其他第三方源码，因此无额外第三方署名要求。

公开服务域名来自实际只读核对：`api.bilibili.com`（`x/web-interface/view`、`x/player/videoshot`、`x/player/v2`）、`i0.hdslb.com`（故事板图片）；`https://b23.tv/BV1xx411c7mD` 实测返回 302 与 `https://www.bilibili.com/video/BV1xx411c7mD`，因此登记 `b23.tv`、`www.bilibili.com`，并按手机端链接兼容登记 `m.bilibili.com`。不包含登录、Cookie、账号互动或绕过风控逻辑。若后续需要新域名（例如特定字幕 CDN），须先取得真实链接证据、更新清单并由管理员批准。
