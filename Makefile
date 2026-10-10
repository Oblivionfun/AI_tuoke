.DEFAULT_GOAL := help
.PHONY: help install demo serve worker test lint fmt check up

help: ## 列出常用命令
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  make %-8s %s\n", $$1, $$2}'

install: ## 安装依赖
	uv sync

demo: ## 建库、导入样册、载入演示数据（重复执行是安全的）
	uv run workbench init
	uv run workbench demo seed

serve: ## 启动网页 http://127.0.0.1:8000
	uv run workbench serve

worker: ## 启动 worker（另开一个终端）
	uv run workbench worker

test: ## 跑测试（不联网，用脚本化的 agent）
	uv run pytest

lint: ## 代码检查
	uv run ruff check src tests
	uv run ruff format --check src tests

fmt: ## 自动格式化
	uv run ruff format src tests
	uv run ruff check --fix src tests

check: lint test ## 提交前跑一遍

up: ## Docker Compose 启动 web + worker + caddy
	docker compose up -d --build
