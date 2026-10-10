# 冠维演示数据

这里的 `dossiers_*.json` 是阿联酋、南非、沙特的真实客户档案，用于演示（`workbench demo seed`）和评测集初标。

- **怎么来的**：研究子代理按 `prompts/prospector.md`、`prompts/researcher.md` 同一份契约联网调研（打开官网首页、产品页、About、Contact），然后由另一个干净上下文的审查子代理按 `prompts/reviewer.md` 点开来源复核。没有 API key 时，用这种方式代替系统 agent 实跑。
- **可信度**：每条结论都附了打开过的页面；查不到的写"未找到"。但这仍是 AI 调研结果，联系前请业务员抽查来源。
- **格式**：`{"country": "AE", "items": [{"candidate": Candidate, "dossier": CompanyDossier, "review": ReviewResult | null}]}`，字段定义见 `src/workbench/agents/schemas.py`。校验：`uv run workbench demo validate`。
