import os
import pytest
from backend.agent_loop.contracts import LoopConfig
from backend.review_contract import ReviewConfig
from test_stage6_integration import environment
pytestmark=[pytest.mark.asyncio,pytest.mark.skipif(os.getenv('RUN_DB_TESTS')!='1',reason='需要隔离PG')]
async def env(**extra):
    return await environment(runtime_version='agent-loop-v1',loop_config=LoopConfig.read('configs/loops/loop-v8-gap-view.v1.8.0.json'),review_config=ReviewConfig(id='gap-test',postcheck=True),**extra)
@pytest.mark.parametrize('kind',['adaptive_evidence','review_rewrite','review_lookup'])
async def test_v8_mechanisms(monkeypatch,kind):
    import test_loop_mechanisms_integration as prior
    monkeypatch.setattr(prior,'env',env)
    await prior.test_separate_mechanism_seams(monkeypatch,kind)
