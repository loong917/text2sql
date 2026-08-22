"""Ollama implementation of the SQL Generator port."""

from __future__ import annotations

import asyncio

import ollama


class OllamaSqlGenerator:
    def __init__(
        self,
        *,
        host: str,
        timeout_seconds: float,
        model: str,
        max_concurrency: int,
        num_ctx: int,
        num_predict: int,
        keep_alive: str,
        refusal_token: str,
    ):
        self._client = ollama.AsyncClient(host=host, timeout=timeout_seconds)
        self._model = model
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._num_ctx = num_ctx
        self._num_predict = num_predict
        self._keep_alive = keep_alive
        self._refusal_token = refusal_token

    async def generate(self, prompt: str) -> str:
        async with self._semaphore:
            response = await self._client.chat(
                model=self._model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你是 SQL Server Text2SQL 生成器。严格执行给定语义 IR、实时 "
                            "Schema 和业务规则；不得补造字段或实体值。只输出一条只读 "
                            f"T-SQL，无法确定时输出 {self._refusal_token}。"
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                options={
                    "temperature": 0,
                    "num_ctx": self._num_ctx,
                    "num_predict": self._num_predict,
                },
                keep_alive=self._keep_alive,
            )
        return response.get("message", {}).get("content", "")
