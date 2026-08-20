# Video OS

**面向 AI Agent 的自改进视频生产引擎。**

素材 → Perception → Plan → Render → QA → Review → Repair → 可信经验

[安装](docs/INSTALL.md) · [快速开始](docs/QUICKSTART.md) ·
[架构](docs/ARCHITECTURE.md) · [视频理解](docs/PERCEPTION.md)

Video OS 将脚本和本地素材转化为确定性剪辑计划与经过真实验证的竖屏视频。
Codex、Claude Code、Gemini CLI 等 Agent 通过统一 CLI 调用，Core 不依赖某个
特定 Agent。

核心特点：

- Agent 原生：CLI 覆盖 run、status、repair、feedback、doctor 和脱敏报告。
- 失败关闭：真实 Perception、媒体解码、QA 和绑定当前视频签名的 Review
  全部成立后，才允许 FINAL。
- 自修复：Review 返回 fix 后进入有限重试的 Repair → Render → QA → 新 Review。
- 可信学习：只有 production_verified 证据能进入人工治理的规则链；Memory
  仍是 advisory，不是硬约束。
- 可替换 Perception：可选独立 Gemini Browser Worker 或 Qwen API，二者都不能
  绕过现有 Perception 合约。

```powershell
cd produce-seeding-video
npm ci --ignore-scripts # 仅 Gemini Browser Worker 需要；不会下载浏览器
python scripts/video_os.py setup --data-root "$env:LOCALAPPDATA\VideoOS" --provider gemini-worker
python scripts/video_os.py doctor
python scripts/video_os.py run C:\你的项目 --to FINAL
```

不要直接改状态文件，也不要伪造 Perception、QA、Review、Repair、生产证据、
规则、激活记录或签名。详见 [AGENTS.md](AGENTS.md)。

Video OS 采用 [GNU Affero General Public License v3.0 or later](LICENSE)
（`AGPL-3.0-or-later`）许可。第三方组件继续适用各自许可证；来源与许可清单见
[THIRD_PARTY_NOTICES.md](produce-seeding-video/THIRD_PARTY_NOTICES.md)。
