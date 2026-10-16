"""v4工具失败、操作恢复与公开提示的共同策略。"""
BUSINESS_BLOCKS = {'application_before_signed', 'return_window_manual_review', 'gift_not_refundable',
    'quality_verification_required', 'eligibility_not_confirmed', 'selection_invalid', 'gift_manual_review'}


def tool_policy(result):
    status = result.get('status')
    code = (result.get('error') or {}).get('code')
    if status in {'success', 'no_result'}:
        return dict(category=status, reexecute=False, resumable=False, terminal=False)
    if status in {'missing_input', 'no_permission'}:
        return dict(category=status, reexecute=False, resumable=False, terminal=True)
    if status == 'permanent_failure' and code in BUSINESS_BLOCKS:
        return dict(category='business_boundary', reexecute=False, resumable=False, terminal=True)
    if status == 'transient_failure':
        return dict(category='technical_transient', reexecute=True, resumable=True, terminal=False)
    return dict(category='technical_permanent', reexecute=False, resumable=False, terminal=True)


def is_v4(frozen):
    return (frozen.get('loop_config') or {}).get('contract_version') in {'loop-contract-conditions-v4','loop-call-bound-v5','loop-scoped-view-v6','loop-gap-full-v7','loop-gap-view-v8','loop-evidence-v9'}


def resumable_error(code):
    if not code or 'budget_exceeded' in code:
        return False
    if code in {'loop_protocol_exhausted', 'loop_tool_permanent_failure', 'runtime_fingerprint_mismatch',
                'business_snapshot_changed', 'knowledge_index_unavailable', 'operation_argument_conflict'}:
        return False
    # 未持久响应的传输/进程故障仍由显式恢复重执行；未知永久结果单独封口。
    return code in {'loop_tool_technical_failure', 'loop_tool_timeout', 'agent_runtime_failed',
                    'lease_expired_explicit_resume', 'process_shutdown_explicit_resume', 'model_timeout',
                    'model_connection_error'} or code.startswith('fixture_')
