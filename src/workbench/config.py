"""工作台配置：全部来自环境变量（前缀 WB_）或 .env。OPENAI_API_KEY 由 Agents SDK 直接读取。"""

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from workbench.agents.schemas import AgentRole

DEFAULT_DIRECTORY_DOMAINS = [
    "alibaba.com",
    "aliexpress.com",
    "made-in-china.com",
    "globalsources.com",
    "indiamart.com",
    "tradeindia.com",
    "tradekey.com",
    "ec21.com",
    "exporthub.com",
    "go4worldbusiness.com",
    "kompass.com",
    "dnb.com",
    "zoominfo.com",
    "rocketreach.co",
    "apollo.io",
    "lusha.com",
    "signalhire.com",
    "contactout.com",
    "yellowpages.ae",
    "yellowpages.co.za",
    "yellosa.co.za",
    "yelu.ae",
    "uaeplusplus.com",
    "dubaicityinfo.com",
    "cybo.com",
    "infobel.com",
    "bizcommunity.com",
    "brabys.com",
    "snupit.co.za",
    "dalil.com",
    "daleeli.com",
    "connect.ae",
    "volza.com",
    "importgenius.com",
    "panjiva.com",
    "importyeti.com",
    "tradeatlas.com",
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "x.com",
    "twitter.com",
    "youtube.com",
    "wikipedia.org",
    "amazon.ae",
    "amazon.sa",
    "noon.com",
    "takealot.com",
]


class WorkbenchSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WB_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///data/workbench.db"
    prompts_dir: Path = Path("prompts")
    knowledge_dir: Path = Path("knowledge")
    csl_path: Path = Path("data/csl.csv")

    # 模型网关。不填走 OpenAI 官方（读 OPENAI_API_KEY）。
    # 公司 AIHub：base_url=http://api.aihub.woa.com/v1，provider=openai（透传 Responses 接口，支持 web_search）。
    # 不要用 /standard/v1/chat/completions：那里 function 工具和联网搜索都返回 400。
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_provider: str = ""
    # 经 SSH 隧道访问网关时，base_url 是隧道的本地端口，这里填网关真实域名作为 Host 头
    llm_host_header: str | None = None

    model: str = "gpt-5.5"
    model_prospector: str | None = None
    model_researcher: str | None = None
    model_reviewer: str | None = None
    model_writer: str | None = None
    model_judge: str | None = None
    reasoning_effort: str = "high"
    search_context_size: str = "medium"

    max_turns_prospector: int = 40
    max_turns_researcher: int = 30
    max_turns_reviewer: int = 25
    max_turns_writer: int = 3
    max_turns_judge: int = 3

    research_concurrency: int = Field(4, ge=1, le=8)
    prospect_batch_size: int = 15
    daily_candidate_limit: int = 40
    daily_review_limit: int = 10
    max_revisions: int = 1

    directory_domains: list[str] = Field(default_factory=lambda: list(DEFAULT_DIRECTORY_DOMAINS))

    tracing_disabled: bool = False
    trace_include_sensitive_data: bool = True

    price_input_per_mtok: float = 0.0
    price_cached_input_per_mtok: float = 0.0
    price_output_per_mtok: float = 0.0
    price_web_search_per_call: float = 0.01

    seller_company: str = "Handan Guanwei Fastener Manufacturing Co., Ltd."
    seller_contact_name: str = ""
    seller_email: str = ""
    seller_whatsapp: str = ""
    seller_website: str = ""

    session_secret: str = "change-me"
    cookie_secure: bool = False
    worker_poll_seconds: float = 2.0
    timezone: str = "Asia/Shanghai"

    @property
    def api_key(self) -> str:
        """传给 OpenAI SDK 的 key。AIHub 的厂商参数拼在 key 后面：<key>?provider=openai。"""
        key = self.llm_api_key or os.environ.get("OPENAI_API_KEY", "")
        return f"{key}?provider={self.llm_provider}" if key and self.llm_provider else key

    @property
    def tracing_enabled(self) -> bool:
        """trace 只能上报到 OpenAI 官方后台；走公司网关时关闭。"""
        return not self.tracing_disabled and not self.llm_base_url

    def model_for(self, role: AgentRole) -> str:
        override = {
            AgentRole.PROSPECTOR: self.model_prospector,
            AgentRole.RESEARCHER: self.model_researcher,
            AgentRole.REVIEWER: self.model_reviewer,
            AgentRole.WRITER: self.model_writer,
            AgentRole.JUDGE: self.model_judge,
        }[role]
        return override or self.model

    def max_turns_for(self, role: AgentRole) -> int:
        return {
            AgentRole.PROSPECTOR: self.max_turns_prospector,
            AgentRole.RESEARCHER: self.max_turns_researcher,
            AgentRole.REVIEWER: self.max_turns_reviewer,
            AgentRole.WRITER: self.max_turns_writer,
            AgentRole.JUDGE: self.max_turns_judge,
        }[role]


@lru_cache(maxsize=1)
def get_settings() -> WorkbenchSettings:
    load_dotenv()
    return WorkbenchSettings()
