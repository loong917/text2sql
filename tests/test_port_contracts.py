"""Structural contract tests for application ports and infrastructure adapters."""

import unittest

from src.application.ports import Generator, Retriever, SqlExecutor, Validator
from src.infrastructure.query_adapters import CallableRetriever, CallableValidator


async def _retrieve(_question):
    return {}


def _validate(_sql, _schema, **_context):
    return None


class _Generator:
    async def generate(self, prompt):
        return prompt


class _Executor:
    async def execute(self, sql, *, timeout_seconds):
        return [sql, timeout_seconds]


class PortContractTests(unittest.TestCase):
    def test_infrastructure_adapters_satisfy_application_contracts(self):
        self.assertIsInstance(CallableRetriever(_retrieve), Retriever)
        self.assertIsInstance(CallableValidator(_validate), Validator)
        self.assertIsInstance(_Generator(), Generator)
        self.assertIsInstance(_Executor(), SqlExecutor)

    def test_missing_method_does_not_satisfy_contract(self):
        self.assertNotIsInstance(object(), Retriever)
        self.assertNotIsInstance(object(), Generator)
