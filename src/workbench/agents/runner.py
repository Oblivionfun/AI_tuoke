"""Agents SDK 薄封装。

只用 SDK 的 Agent、Runner、output_type、托管 WebSearchTool 和 tracing。
模型走 Responses 接口：OpenAI 官方，或公司 AIHub 网关的 OpenAI 透传接口（见 config.llm_base_url）。
任务状态、步骤和用量都存进自己的数据库，不用 SDK 的 Session。
测试和离线环境用 ScriptedRunner 代替真实调用。
"""

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from agents import (
    Agent,
    ModelSettings,
    OpenAIResponsesModel,
    RunConfig,
    RunItemStreamEvent,
    Runner,
    ToolCallItem,
    WebSearchTool,
    gen_trace_id,
)
from agents.exceptions import AgentsException, MaxTurnsExceeded
from openai import AsyncOpenAI
from openai.types.responses.web_search_tool_param import UserLocation
from openai.types.shared import Reasoning
from pydantic import BaseModel

from workbench.agents.schemas import ROLE_LABELS, AgentRole
from workbench.config import WorkbenchSettings

TRACE_URL = "https://platform.openai.com/traces/trace?trace_id={trace_id}"

StepCallback = Callable[[str, str, dict[str, Any]], Awaitable[None] | None]


@dataclass
class AgentSpec:
    role: AgentRole
    instructions: str
    output_type: type[BaseModel]
    model: str
    max_turns: int
    web_search: bool
    reasoning_effort: str | None = None
    search_context_size: str = "medium"
    country: str | None = None
    prompt_version: str = ""


@dataclass
class UsageRecord:
    requests: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    web_search_calls: int = 0

    def cost_usd(self, settings: WorkbenchSettings) -> float:
        uncached = max(self.input_tokens - self.cached_input_tokens, 0)
        return round(
            uncached / 1e6 * settings.price_input_per_mtok
            + self.cached_input_tokens / 1e6 * settings.price_cached_input_per_mtok
            + self.output_tokens / 1e6 * settings.price_output_per_mtok
            + self.web_search_calls * settings.price_web_search_per_call,
            4,
        )

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass
class AgentResult:
    output: BaseModel
    usage: UsageRecord
    trace_id: str | None = None


class AgentRunError(RuntimeError):
    def __init__(self, message: str, usage: UsageRecord | None = None, trace_id: str | None = None):
        super().__init__(message)
        self.usage = usage or UsageRecord()
        self.trace_id = trace_id


class AgentRunner(Protocol):
    async def run(
        self,
        spec: AgentSpec,
        input_text: str,
        *,
        on_step: StepCallback,
        trace_group: str | None = None,
        trace_metadata: dict[str, Any] | None = None,
    ) -> AgentResult: ...


async def emit(on_step: StepCallback, kind: str, text: str, data: dict[str, Any] | None = None) -> None:
    result = on_step(kind, text, data or {})
    if inspect.isawaitable(result):
        await result


def describe_web_search(raw: Any) -> tuple[str, str, dict[str, Any]] | None:
    """把一次 web_search_call 翻译成界面上的一行步骤："搜索：…" / "打开：…" / "页内查找：…"。"""
    action = getattr(raw, "action", None)
    if action is None:
        return None
    kind = getattr(action, "type", "")
    if kind == "search":
        queries = list(getattr(action, "queries", None) or [])
        query = getattr(action, "query", None)
        if query and query not in queries:
            queries.insert(0, query)
        text = "；".join(queries) if queries else "（未返回查询词）"
        return "search", f"搜索：{text}", {"queries": queries}
    if kind == "open_page":
        url = getattr(action, "url", "") or ""
        return "open", f"打开：{url}", {"url": url}
    if kind == "find":
        url = getattr(action, "url", "") or ""
        pattern = getattr(action, "pattern", "") or ""
        return "find", f"页内查找：{pattern} @ {url}", {"url": url, "pattern": pattern}
    return None


def _usage_from(result: Any, web_search_calls: int) -> UsageRecord:
    usage = result.context_wrapper.usage
    return UsageRecord(
        requests=usage.requests,
        input_tokens=usage.input_tokens,
        cached_input_tokens=getattr(usage.input_tokens_details, "cached_tokens", 0) or 0,
        output_tokens=usage.output_tokens,
        reasoning_tokens=getattr(usage.output_tokens_details, "reasoning_tokens", 0) or 0,
        web_search_calls=web_search_calls,
    )


def openai_client(settings: WorkbenchSettings) -> AsyncOpenAI:
    """OpenAI 官方或公司网关（AIHub）的客户端。两者都走 Responses 接口，只是地址和 key 不同。"""
    headers = {"Host": settings.llm_host_header} if settings.llm_host_header else None
    return AsyncOpenAI(api_key=settings.api_key or None, base_url=settings.llm_base_url or None, default_headers=headers)


class SdkRunner:
    """用 openai-agents 真正跑 agent：流式读取步骤，结束后取结构化结果和用量。"""

    def __init__(self, settings: WorkbenchSettings):
        self.settings = settings
        self._client: AsyncOpenAI | None = None

    @property
    def client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = openai_client(self.settings)
        return self._client

    def build_agent(self, spec: AgentSpec) -> Agent:
        tools = []
        if spec.web_search:
            location = UserLocation(type="approximate", country=spec.country) if spec.country else None
            tools.append(WebSearchTool(user_location=location, search_context_size=spec.search_context_size))  # type: ignore[arg-type]
        settings = (
            ModelSettings(reasoning=Reasoning(effort=spec.reasoning_effort))
            if spec.reasoning_effort
            else ModelSettings()
        )  # type: ignore[arg-type]
        return Agent(
            name=f"{ROLE_LABELS[spec.role]}agent",
            instructions=spec.instructions,
            model=OpenAIResponsesModel(model=spec.model, openai_client=self.client),
            model_settings=settings,
            tools=tools,
            output_type=spec.output_type,
        )

    async def run(
        self,
        spec: AgentSpec,
        input_text: str,
        *,
        on_step: StepCallback,
        trace_group: str | None = None,
        trace_metadata: dict[str, Any] | None = None,
    ) -> AgentResult:
        trace_id = gen_trace_id() if self.settings.tracing_enabled else None
        run_config = RunConfig(
            workflow_name=f"冠维工作台-{ROLE_LABELS[spec.role]}",
            trace_id=trace_id,
            group_id=trace_group,
            trace_metadata={"prompt_version": spec.prompt_version, **(trace_metadata or {})},
            tracing_disabled=not self.settings.tracing_enabled,
            trace_include_sensitive_data=self.settings.trace_include_sensitive_data,
        )
        result = Runner.run_streamed(
            self.build_agent(spec), input_text, max_turns=spec.max_turns, run_config=run_config
        )
        searches = 0
        try:
            async for event in result.stream_events():
                if not isinstance(event, RunItemStreamEvent):
                    continue
                if event.name == "tool_called" and isinstance(event.item, ToolCallItem):
                    raw = event.item.raw_item
                    if getattr(raw, "type", None) == "web_search_call":
                        searches += 1
                        described = describe_web_search(raw)
                        if described:
                            await emit(on_step, *described)
                elif event.name == "message_output_created":
                    await emit(on_step, "message", "整理结果")
        except MaxTurnsExceeded as exc:
            raise AgentRunError(f"超过最大轮数 {spec.max_turns}", _usage_from(result, searches), trace_id) from exc
        except AgentsException as exc:
            raise AgentRunError(f"Agent 运行失败：{exc}", _usage_from(result, searches), trace_id) from exc
        output = result.final_output
        if not isinstance(output, spec.output_type):
            raise AgentRunError("Agent 没有交回结构化结果", _usage_from(result, searches), trace_id)
        return AgentResult(output=output, usage=_usage_from(result, searches), trace_id=trace_id)


async def live_check(settings: WorkbenchSettings) -> str:
    """真实调用一次模型 + 联网搜索，确认网关、key、型号和 web_search 都可用。失败时抛出异常。"""
    agent = Agent(
        name="体检",
        instructions="用一句话回答。",
        model=OpenAIResponsesModel(model=settings.model, openai_client=openai_client(settings)),
        tools=[WebSearchTool()],
    )
    run_config = RunConfig(tracing_disabled=not settings.tracing_enabled)
    result = await Runner.run(
        agent, "搜索 ITAC South Africa 官网首页的网址是什么？", max_turns=4, run_config=run_config
    )
    return str(result.final_output)


ScriptFn = Callable[[AgentSpec, str], BaseModel]


@dataclass
class ScriptedRunner:
    """按角色返回预设结果的假 runner，用于测试和离线演示；同时记下每次调用的输入，方便断言。"""

    scripts: dict[AgentRole, ScriptFn | list[BaseModel]]
    calls: list[tuple[AgentRole, str]] = field(default_factory=list)

    async def run(
        self,
        spec: AgentSpec,
        input_text: str,
        *,
        on_step: StepCallback,
        trace_group: str | None = None,
        trace_metadata: dict[str, Any] | None = None,
    ) -> AgentResult:
        self.calls.append((spec.role, input_text))
        script = self.scripts.get(spec.role)
        if script is None:
            raise AgentRunError(f"没有为 {spec.role} 准备脚本")
        if spec.web_search:
            await emit(on_step, "search", f"搜索：（脚本）{ROLE_LABELS[spec.role]}", {"queries": []})
        output = script.pop(0) if isinstance(script, list) else script(spec, input_text)
        await emit(on_step, "message", "整理结果")
        return AgentResult(output=output, usage=UsageRecord(requests=1, web_search_calls=int(spec.web_search)))
