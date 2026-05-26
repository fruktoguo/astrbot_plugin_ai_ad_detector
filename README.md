# AI 广告识别

AstrBot 群聊广告识别插件。插件会把群消息的文本、图片和发送者昵称交给配置的 AI 模型判断，命中后可选择只记录、群内通知、撤回消息或撤回并通知。

## 功能

- 按群配置监控规则。
- 全局默认模型，也支持每个群单独指定模型。
- 默认广告识别提示词，可在 WebUI 配置中改写。
- 支持识别文本广告、图片广告、二维码/联系方式/推广海报，以及昵称广告。
- 支持 `log`、`notify`、`recall`、`recall_and_notify` 四种命中动作。

## 配置

在 AstrBot 插件管理页面打开本插件配置：

- `basic.default_action`：默认命中动作。
- `basic.confidence_threshold`：默认置信度阈值，建议从 `0.72` 开始。
- `basic.skip_command_messages`：跳过 `/` 开头的命令消息，避免状态命令触发 AI 审核。
- `llm.provider_id`：默认模型。图片识别需要选择支持视觉的模型。
- `prompts.ad_detection_prompt`：广告识别提示词。可使用 `${payload_json}` 插入待审核消息 JSON。
- `monitors`：群聊监控规则。每条规则填写一个 `group_id`，可单独覆盖 `provider_id`、`action`、`confidence_threshold`。

## 命令

- `/广告检测状态`：查看插件开关、监控规则数和当前群是否被监控。

## 注意

撤回消息依赖 OneBot 的 `delete_msg` 能力，并且机器人必须有足够权限。模型误判时建议先使用 `notify` 观察，再切换到 `recall_and_notify`。
