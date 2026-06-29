# wechat-ai-persona

在微信里部署一个有人格、有记忆、能连续对话的 AI 伴侣。  
基于 OpenAI 兼容 API，支持任意模型和中转站。

---

## 效果

- 微信收到消息 → AI 以自定义人格回复
- 短期记忆：滚动对话历史，超长自动压缩
- 长期记忆：AI 主动调用工具存取重要信息
- 语音气泡：TTS 生成 SILK 格式语音发送
- 多条气泡：自然分段，不是一整块文字

---

## 前置要求

- Linux / WSL2（Windows 用户）
- Node.js >= 18
- Python 3.10+
- ffmpeg（语音功能需要）
- 微信 iOS 最新版（需支持 ClawBot）
- OpenAI 兼容 API 的 base_url + key

---

## 快速开始

### 1. 微信登录

```bash
npx claude-code-wechat-channel setup
```

终端显示二维码，微信扫码确认。

### 2. 安装依赖

```bash
npm install
pip install openai python-dotenv pilk
```

### 3. 配置

```bash
cp .env.example .env
```

编辑 `.env`，填入你的 API 信息：

```
OPENAI_BASE_URL=https://your-api-base-url/v1
OPENAI_API_KEY=sk-xxxxxx
OPENAI_MODEL=gpt-4o
```

### 4. 写人格文件

```bash
cp PERSONA.md.example PERSONA.md
```

编辑 `PERSONA.md`，定义 AI 的身份、性格、说话方式。

### 5. 启动

```bash
./start.sh
```

微信发消息，AI 回复。

---

## 人格设计

`PERSONA.md` 是核心。给框架，不给脚本——定义性格和边界，不要逐字规定怎么说话。

参考 `PERSONA.md.example`。

---

## 记忆系统

**短期记忆**：`data/wechat_history.json`，保留最近 N 条对话（`MAX_HISTORY` 控制），超出自动压缩成摘要存入长期记忆。

**长期记忆**：SQLite（`data/memory.db`），AI 可主动调用三个工具：

- `memory_remember` — 存一条记忆
- `memory_search` — 搜索记忆
- `memory_list` — 列出最近记忆

---

## 语音

模型回复里加 `[VOICE:文字]` 触发中文语音，`[VOICE_EN:text]` 触发英文语音。

流程：TTS → MP3 → ffmpeg → PCM → pilk → SILK → 微信语音气泡。

---

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `OPENAI_BASE_URL` | API base URL | 必填 |
| `OPENAI_API_KEY` | API key | 必填 |
| `OPENAI_MODEL` | 模型名 | `gpt-4o` |
| `PERSONA_NAME` | AI 名字 | `Cass` |
| `USER_NAME` | 用户名字 | `用户` |
| `MEMORY_DB_PATH` | 记忆数据库路径 | `./data/memory.db` |
| `MAX_HISTORY` | 最大对话历史条数 | `40` |

---

## 项目结构

```
├── standalone_wechat_cass.js   # 微信消息监听主进程
├── cass_openai_client.py       # prompt 构建 + API 调用
├── cass_once.py                # 单次消息处理
├── send_wechat_once.js         # 消息发送 + 气泡切割
├── backend_router.py           # API 路由
├── memory/
│   ├── memory_store.py         # SQLite 读写
│   ├── memory_tools.py         # 工具定义
│   └── compactor.py            # 对话历史压缩
├── PERSONA.md                  # 你的人格文件（自己写）
├── PERSONA.md.example          # 人格模板
└── .env                        # 配置（自己填）
```

---

## 致谢

- [Johnixr/claude-code-wechat-channel](https://github.com/Johnixr/claude-code-wechat-channel) — 微信 iLink Bot 协议实现，本项目的消息收发基础
- [lith0924/wechat-ilink-sdk-java](https://github.com/lith0924/wechat-ilink-sdk-java) — 语音气泡关键参数参考（`encrypt_type=1`）

---

## License

MIT

**Author:** Sylvia Dong
