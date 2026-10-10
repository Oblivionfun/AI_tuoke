# 冠维获客工作台

给邯郸冠维紧固件做的海外客户研究工作台，原则是"先查实、再出手"：每天给业务员最多 10 张查实过的海外批发商卡片，每张附首封草稿和联系路线。联系谁由人决定，邮件由业务员从企业邮箱手动发送，系统负责记录回复和询价。

- 4 个 agent（寻客、调研、审查、文案），OpenAI Agents SDK 只用薄薄一层，主流程由代码编排；
- 效果靠提示词契约（`prompts/`）、分层知识包（`knowledge/`）和干净上下文的审查 agent，代码不校验 agent 的结论；
- 三条底线：不自动发送、拒绝联系名单在寻客和去重两处生效、每次运行都有轮数和每日上限并记账。

## 跑起来

```bash
make install                     # 需要 uv 和 Python 3.12
cp .env.example .env             # 填 OPENAI_API_KEY；本地把 WB_COOKIE_SECURE 改成 false
make demo                        # 建库、导入样册、载入 18 家真实客户的演示档案
uv run workbench user-add 张三     # 创建登录账号
make serve                       # http://127.0.0.1:8000
make worker                      # 另一个终端：执行寻客、调研、文案任务
```

`make` 列出全部常用命令，`uv run workbench --help` 列出全部命令行。

## 文档

- [ARCHITECTURE.md](ARCHITECTURE.md)：代码地图、数据流、不变量。读代码从这里开始。
- [方案总览](docs/01-方案总览.md)：核心思路、两级队列、联系渠道、agent 编排
- [市场与合规](docs/02-市场与合规.md)：阿联酋、南非（反倾销、POPIA）、沙特
- [给老板的演示脚本](docs/03-演示脚本.md)
- [运行手册](docs/04-运行手册.md)：部署、日常使用、每周提示词复盘
- `docs/原始资料/`：项目最初的实施方案、开发任务书和 V1.5 原型

## 开发

```bash
make check                       # ruff 检查 + 测试（不联网，几秒跑完）
make fmt                         # 自动格式化
```

**免责声明**：市场与合规说明来自公开资料整理，不构成法律意见；试点前请律师复核可信度标"低"的条目。
