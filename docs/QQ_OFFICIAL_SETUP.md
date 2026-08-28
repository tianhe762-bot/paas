# QQ 官方机器人接入

本系统使用 **QQ 开放平台官方机器人**（q.qq.com），不依赖 OneBot/NapCat 等中转，AppID + AppSecret 直连官方网关。

## 申请机器人

1. 登录 [QQ 开放平台](https://q.qq.com/) → 机器人 → 创建机器人应用。
2. 在「开发 → 开发设置」中获取：
   - **AppID**
   - **AppSecret**
3. 按平台要求完成开发者资质认证（个人/企业），并按需确认开通单聊（C2C）消息权限。
4. 在 QQ 中搜索并添加自己的机器人（沙箱环境亦可先联调）。

## 在 PAAS 管理界面配置

1. 打开 `/admin` → 平台配置 → QQ。
2. 填写 AppID、AppSecret。
3. 点「测试连接」：PAAS 会调用 `bots.qq.com/app/getAppAccessToken` 换取 access_token 验证凭证。
4. 勾选启用 → 保存：PAAS 通过 WebSocket 连接官方网关（`intents = 1<<25`，订阅 `C2C_MESSAGE_CREATE`），保存即热生效。

## 消息与文件

- 用户单聊（C2C）消息事件直接推送到 PAAS；回复通过 `POST /v2/users/{openid}/messages` 发送（携带 `msg_id`/`msg_seq` 走被动回复，过期自动降级为主动发送）。
- 用户发送 `.csv` / `.xlsx` 文件时，事件 `attachments[].url` 提供下载地址，PAAS 自动下载并导入账本。
- 主动消息受平台频控（未认证 5 QPS / 30 QPM / 单关系 20 QPM），提醒任务仅发给最近 30 天互动过的会话。

## 常见错误

| 现象 | 处理 |
|---|---|
| 测试连接失败 | 检查 AppID/AppSecret 是否复制完整、应用是否已通过审核 |
| 收不到消息 | 确认 C2C 事件权限已开通，机器人未处于沙箱外未发布状态 |
| 发送失败 `4005xxxx` | 检查是否触发频控；被动回复超时（单聊 60 分钟）会自动转主动发送 |

