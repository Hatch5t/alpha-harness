"""Fault injection through the SDK, service, strict parser and durable controller.

Every HTTP request terminates in MockTransport. All data and credentials are synthetic.
"""

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from httpx2 import AsyncClient, ConnectError, MockTransport, ReadTimeout, Request, Response
from pydantic import ValidationError
from sqlalchemy import select

from alpha_harness.db.models import KeyUsage, ResearchCampaign, ResearchEvent, SimulationRecord
from alpha_harness.db.sqlite import Database
from alpha_harness.llm.failures import failure
from alpha_harness.llm.keys import LLMError
from alpha_harness.llm.registry import ModelInfo
from alpha_harness.llm.service import LLMService
from alpha_harness.research.io import ResearchIO
from alpha_harness.research.schemas import CampaignSpec, Generated
from alpha_harness.research.service import ResearchError, ResearchService
from alpha_harness.sealing import Sealer


class GenerationFailureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = Database(Path(folder.name) / "state.db")
        await self.db.create_all()
        self.addAsyncCleanup(self.db.dispose)
        self.model = ModelInfo(
            id="synthetic", provider="google", rpm=1000, rpd=1000, reset_timezone="UTC"
        )
        registry = SimpleNamespace(get=lambda _: self.model, all=lambda: [self.model])
        self.requests: list[dict] = []
        self.http_status = 400
        self.body = {
            "error": {
                "code": 400,
                "message": "The specified schema produces a constraint "
                "that has too many states for serving.",
            }
        }
        self.transport_error = None

        def respond(request: Request) -> Response:
            self.requests.append(json.loads(request.content))
            if self.transport_error:
                raise self.transport_error("Synthetic connection failure", request=request)
            return Response(self.http_status, json=self.body)

        self.http = AsyncClient(transport=MockTransport(respond), trust_env=False)
        self.addAsyncCleanup(self.http.aclose)
        with patch("alpha_harness.llm.service.DefaultAsyncHttpxClient", return_value=self.http):
            self.llm = LLMService(self.db, Sealer(Path(folder.name) / "key"), registry)
        await self.llm.keys.add("synthetic-secret-never-persist", daily_limit=1000)
        self.forbidden = AsyncMock(side_effect=AssertionError("External call forbidden"))
        self.state = SimpleNamespace(
            db=self.db,
            llm=self.llm,
            models=registry,
            engine=SimpleNamespace(enqueue=self.forbidden),
            alphas=SimpleNamespace(by_ids=self.forbidden),
            optimizer=None,
            auth=SimpleNamespace(session=SimpleNamespace(authenticated=False)),
        )
        self.service = ResearchService(self.state)
        await self.service.initialize()
        self.spec = CampaignSpec(
            hypothesis="Synthetic delayed revision mechanism.",
            region="GLB",
            delay=1,
            model=self.model.ref,
            validation_start=date(2025, 1, 1),
            validation_end=date(2025, 12, 31),
        )
        self.live_io = ResearchIO(self.state, offline=False)

    async def call(self) -> object:
        ctx = (await self.service._ios["offline"].contexts(self.spec))[0]
        return await self.live_io.generate(self.spec, ctx, "Synthetic fixture only")

    def completion(self, text: str, *, finish: str = "stop", refusal: str | None = None) -> None:
        self.http_status = 200
        self.body = {
            "id": "synthetic",
            "object": "chat.completion",
            "created": 0,
            "model": "synthetic",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": finish,
                    "message": {"role": "assistant", "content": text, "refusal": refusal},
                }
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
        }

    async def test_exact_rc4_schema_error_retains_two_charges_and_fails_before_simulation(
        self,
    ) -> None:
        ident = await self.service.create(self.spec)
        await self.service.control(ident, "start")
        with patch.object(
            self.service._ios["offline"], "generate", side_effect=self.live_io.generate
        ):
            for _ in range(8):
                await self.service.advance(ident)
        report = await self.service.report(ident, complete=True)
        self.assertEqual((report["status"], report["stage"]), ("FAILED", "FAILED"))
        self.assertEqual(report["candidates"], [])
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(
            report["budgets"]["requests"],
            {"reserved": 2, "knownObserved": 0, "unknownOperations": 2},
        )
        self.assertEqual(
            {f["diagnostic"]["category"] for f in report["generation"]["failures"]},
            {"schema_complexity"},
        )
        self.assertTrue(
            all(f["diagnostic"]["httpStatus"] == 400 for f in report["generation"]["failures"])
        )
        self.assertNotIn("synthetic-secret", json.dumps(report))
        async with self.db.session() as session:
            self.assertEqual(list(await session.scalars(select(SimulationRecord))), [])
            usage = await session.scalar(select(KeyUsage).where(KeyUsage.model == "synthetic"))
            self.assertEqual((usage.requests, usage.tokens, usage.reported_requests), (2, 0, 0))
        with self.assertRaises(ResearchError):
            await self.service.control(ident, "resume")
        self.service = ResearchService(self.state)
        await self.service.tick()
        await self.service.advance(ident)
        self.assertEqual(len(self.requests), 2)
        self.forbidden.assert_not_awaited()

    async def test_fault_matrix_preserves_actual_http_cause_and_never_retries(self) -> None:
        cases = [
            (400, "invalid request", "http_error"),
            (400, "response_format schema unsupported", "request_schema"),
            (401, "unauthenticated", "authentication"),
            (403, "permission denied", "model_access"),
            (404, "model missing", "model_access"),
            (429, "insufficient_quota", "quota"),
            (429, "RESOURCE_EXHAUSTED daily quota", "quota"),
            (429, "too many requests", "rate_limit"),
            (503, "busy", "provider_unavailable"),
        ]
        for index, (status, message, category) in enumerate(cases):
            with self.subTest(status=status, category=category):
                # Separate synthetic key avoids a previous injected quota penalty.
                for key in await self.llm.keys.list_keys():
                    await self.llm.keys.set_enabled(key.id, False)
                await self.llm.keys.add(f"synthetic-{index}-{status}-{category}", daily_limit=1000)
                self.http_status, self.body = (
                    status,
                    {
                        "error": {
                            "message": message
                            + " synthetic-secret-never-persist private-prompt-marker"
                        }
                    },
                )
                before = len(self.requests)
                with self.assertRaises(LLMError) as caught:
                    await self.call()
                diagnostic = caught.exception.diagnostic
                self.assertEqual((diagnostic.category, diagnostic.http_status), (category, status))
                self.assertEqual(len(self.requests), before + 1)
                self.assertNotIn("private-prompt-marker", str(caught.exception))
                self.assertNotIn("synthetic-secret", json.dumps(diagnostic.document()))

    async def test_connection_and_timeout_are_distinct_without_sdk_retries(self) -> None:
        for error, category in [(ConnectError, "connection"), (ReadTimeout, "timeout")]:
            self.transport_error = error
            before = len(self.requests)
            with self.assertRaises(LLMError) as caught:
                await self.call()
            self.assertEqual(caught.exception.diagnostic.category, category)
            self.assertEqual(len(self.requests), before + 1)

    async def test_unusable_successes_keep_known_usage_and_distinct_diagnostics(self) -> None:
        for text, finish, refusal, category in [
            ("not json", "stop", None, "malformed_response"),
            ("{}", "stop", None, "schema_validation"),
            ("[]", "stop", None, "schema_validation"),
            ("", "stop", None, "empty_response"),
            ('{"hypotheses":', "length", None, "output_limit"),
            ("", "stop", "Declined", "refusal"),
        ]:
            with self.subTest(category=category, text=text):
                self.completion(text, finish=finish, refusal=refusal)
                with self.assertRaises(LLMError) as caught:
                    await self.call()
                self.assertEqual(caught.exception.diagnostic.category, category)
                self.assertEqual(
                    caught.exception.observed,
                    {"requests": 1, "tokens": 30, "input": 20, "output": 10},
                )

    async def test_schema_failure_settles_known_usage_in_campaign_without_refunds(self) -> None:
        self.completion("{}")
        ident = await self.service.create(self.spec)
        await self.service.control(ident, "start")
        with patch.object(
            self.service._ios["offline"], "generate", side_effect=self.live_io.generate
        ):
            for _ in range(8):
                await self.service.advance(ident)
        report = await self.service.report(ident)
        self.assertEqual(report["status"], "FAILED")
        self.assertEqual(
            report["budgets"]["requests"],
            {"reserved": 2, "knownObserved": 2, "unknownOperations": 0},
        )
        self.assertEqual(report["budgets"]["tokens"]["knownObserved"], 60)
        self.assertEqual(report["budgets"]["tokens"]["unknownOperations"], 0)
        self.assertGreater(report["budgets"]["tokens"]["reserved"], 60)
        self.forbidden.assert_not_awaited()

    async def test_successful_http_without_usage_does_not_invent_token_counts(self) -> None:
        for text, category in [("not json", "malformed_response"), ("", "empty_response")]:
            self.completion(text)
            self.body.pop("usage")
            with self.assertRaises(LLMError) as caught:
                await self.call()
            self.assertEqual(caught.exception.observed, {"requests": 1})
            self.assertEqual(caught.exception.diagnostic.category, category)
        async with self.db.session() as session:
            usage = await session.scalar(select(KeyUsage).where(KeyUsage.model == "synthetic"))
            self.assertEqual((usage.requests, usage.tokens, usage.reported_requests), (2, 0, 0))

    async def test_malformed_usage_never_credits_tokens_or_masks_the_failure(self) -> None:
        for value in [-30, True, "invalid"]:
            self.completion("{}")
            self.body["usage"]["total_tokens"] = value
            with self.assertRaises(LLMError) as caught:
                await self.call()
            self.assertEqual(caught.exception.diagnostic.category, "malformed_response")
            self.assertEqual(caught.exception.observed, {"requests": 1})
        async with self.db.session() as session:
            usage = await session.scalar(select(KeyUsage).where(KeyUsage.model == "synthetic"))
            self.assertEqual((usage.requests, usage.tokens, usage.reported_requests), (3, 0, 0))

    async def test_compact_wire_schema_keeps_structure_and_strict_local_bounds(self) -> None:
        ctx = (await self.service._ios["offline"].contexts(self.spec))[0]
        generated, _ = await self.service._ios["offline"].generate(self.spec, ctx, "fixture")
        self.completion(json.dumps(generated.model_dump(mode="json")))
        result, usage = await self.call()
        self.assertEqual(result, generated)
        self.assertEqual(usage["tokens"], 30)
        wire = self.requests[0]
        schema = wire["response_format"]["json_schema"]["schema"]
        self.assertEqual(schema, Generated.provider_schema())
        self.assertEqual(schema["required"], ["hypotheses", "alphas"])
        for key in ["minItems", "maxItems", "minLength", "maxLength", "minimum", "maximum"]:
            self.assertNotIn(f'"{key}":', json.dumps(schema))
        self.assertEqual(wire["max_tokens"], self.spec.max_output_tokens)
        for change in [
            lambda document: document.update(alphas=document["alphas"] * 3),
            lambda document: document["alphas"][0].update(hypothesis=5),
            lambda document: document["hypotheses"][0].update(mechanism="short"),
        ]:
            invalid = generated.model_dump(mode="json")
            change(invalid)
            with self.assertRaises(ValidationError):
                Generated.model_validate(invalid)
        # The full schema and prompt contract still carry all bounds.
        self.assertIn('"maxItems": 40', json.dumps(Generated.model_json_schema()))

    async def test_legacy_complete_projection_preserves_original_records(self) -> None:
        ident = await self.service.create(self.spec)
        async with self.db.session() as session:
            row = await session.get(ResearchCampaign, ident)
            row.status, row.cursor = "COMPLETE", {"stage": "COMPLETE", "cycle": 0}
            row.message = "Legacy completion"
            session.add(
                ResearchEvent(
                    campaign_id=ident,
                    key="generation:0:synthetic:failed",
                    kind="provider_failure",
                    payload={"dataset": "synthetic", "errorClass": "LLMError"},
                )
            )
        report = await self.service.report(ident)
        self.assertEqual((report["status"], report["storedStatus"]), ("FAILED", "COMPLETE"))
        self.assertEqual(
            report["generation"]["failures"][0]["diagnostic"], failure("unknown").document()
        )
        original = await self.service.get(ident)
        self.assertEqual((original.status, original.message), ("COMPLETE", "Legacy completion"))

    async def test_partial_failure_retains_candidates_and_does_not_fail_entire_campaign(
        self,
    ) -> None:
        ident = await self.service.create(self.spec)
        await self.service.control(ident, "start")
        io = self.service._ios["offline"]
        original = io.generate
        count = 0

        async def generate(*args: object) -> object:
            nonlocal count
            count += 1
            if count == 1:
                diagnostic = failure("schema_complexity", 400)
                raise LLMError(diagnostic.message, diagnostic=diagnostic)
            return await original(*args)

        with patch.object(io, "generate", side_effect=generate):
            for _ in range(60):
                await self.service.advance(ident)
                if (await self.service.get(ident)).status != "RUNNING":
                    break
        report = await self.service.report(ident)
        self.assertEqual(report["status"], "COMPLETE", report["message"])
        self.assertFalse(report["generation"]["allFailed"])
        self.assertTrue(report["candidates"])
        self.assertEqual(len(report["generation"]["failures"]), 1)
        self.forbidden.assert_not_awaited()
