import json

from agents import OpenAIResponsesModel
from agents.models.openai_responses import Converter

from workbench.agents.runner import AgentSpec, SdkRunner
from workbench.agents.schemas import AgentRole, CompanyDossier
from workbench.config import WorkbenchSettings


def spec() -> AgentSpec:
    return AgentSpec(
        role=AgentRole.RESEARCHER,
        instructions="x",
        output_type=CompanyDossier,
        model="gpt-6.1-sol",
        max_turns=5,
        web_search=True,
        reasoning_effort="high",
        country="AE",
    )


def test_aihub_gateway_uses_responses_passthrough(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    settings = WorkbenchSettings(
        _env_file=None,
        llm_base_url="http://api.aihub.woa.com/v1",
        llm_api_key="secret",
        llm_provider="openai",
        model="gpt-6.1-sol",
    )
    assert settings.api_key == "secret?provider=openai"
    assert not settings.tracing_enabled

    agent = SdkRunner(settings).build_agent(spec())
    assert isinstance(agent.model, OpenAIResponsesModel)
    client = agent.model._client
    assert str(client.base_url).rstrip("/") == "http://api.aihub.woa.com/v1"
    assert client.api_key == "secret?provider=openai"
    assert [type(t).__name__ for t in agent.tools] == ["WebSearchTool"]


def test_official_openai_keeps_tracing(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    settings = WorkbenchSettings(
        _env_file=None,
    )
    assert settings.api_key == "sk-test"
    assert settings.tracing_enabled


def test_web_search_request_shape(monkeypatch):
    """网关按 OpenAI 规范校验：user_location 必须是对象（曾因传 Pydantic 模型被序列化成数组而 400）。"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    agent = SdkRunner(
        WorkbenchSettings(
            _env_file=None,
        )
    ).build_agent(spec())
    converted, _ = Converter._convert_tool(agent.tools[0])
    assert converted["type"] == "web_search"
    assert converted["user_location"] == {"type": "approximate", "country": "AE"}
    assert json.loads(json.dumps(converted))["user_location"]["country"] == "AE"
