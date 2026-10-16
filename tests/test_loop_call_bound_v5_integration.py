import os
import pytest
from backend.agent_loop.contracts import LoopConfig
from backend.review_contract import ReviewConfig
from test_stage6_integration import environment
pytestmark=[pytest.mark.asyncio,pytest.mark.skipif(os.getenv('RUN_DB_TESTS')!='1',reason='需要隔离PG')]

async def env(**extra):
    return await environment(runtime_version='agent-loop-v1',loop_config=LoopConfig.read('configs/loops/loop-v5-call-bound.v1.5.0.json'),review_config=ReviewConfig(id='call-bound-test',postcheck=True),**extra)

@pytest.mark.parametrize('kind',['adaptive_evidence','review_rewrite','review_lookup'])
async def test_v5_mechanisms(monkeypatch,kind):
    import test_loop_mechanisms_integration as prior
    monkeypatch.setattr(prior,'env',env)
    await prior.test_separate_mechanism_seams(monkeypatch,kind)

@pytest.mark.parametrize('point',['candidate_created','review_result'])
async def test_v5_durable(monkeypatch,point):
    import test_loop_recovery_v3_integration as prior
    monkeypatch.setattr(prior,'env',env)
    await prior.test_recovery_crash_reuses_durable_outputs(monkeypatch,point)

async def test_v5_cancel(monkeypatch):
    import test_agent_loop_integration as prior
    monkeypatch.setattr(prior,'env',env)
    await prior.test_cancel_after_review_before_publish(monkeypatch)
