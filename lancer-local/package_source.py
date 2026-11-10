#!/usr/bin/env python3
"""Build a reviewable source archive without workspace data or local settings."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

HERE = Path(__file__).resolve().parent
PREFIX = 'yuki-source-preview'
SOURCE_FILES = (
    'trigger_continuation.py', 'test_trigger_subscriptions.py', 'test_trigger_subscription_boundaries.py',
    'test_github_subscriptions.py', 'test_worker_process_continuation.py',
    'trigger_policy.py',
    'trigger_store.py',
    'trigger_engine.py',
    'trigger_service.py',
    'trigger_tasks.py',
    'github_source.py',
    'github_delivery.py',
    'worker_subagents.py', 'child_workspace.py',
    'test_trigger_policy.py',
    'test_triggers.py',
    'test_trigger_service.py', 'test_trigger_readiness.py',
    'test_trigger_github_flow.py',
    'test_github_source.py',
    'test_github_delivery.py',
    'test_worker_subagents.py', 'test_worker_child_adoption.py',
    'execution_capacity.py',
    'task_dispatch.py',
    'task_routing.py',
    'task_route_runtime.py',
    'test_execution_capacity.py',
    'test_task_routing.py',
    'test_task_route_runtime.py',
    'test_task_dispatch.py',
    'worker_integration.py', 'worker_review_evidence.py',
    'worker_policy.py', 'worker_process_policy.py', 'worker_process.py',
    'worker_workspace.py',
    'worker_execution.py',
    'test_worker_policy.py', 'test_worker_process.py', 'test_worker_process_evidence.py',
    'test_worker_integration.py', 'test_worker_review_evidence.py',
    'test_worker_workspace.py',
    'test_worker_execution.py',
    'review_requirements.py',
    'review_evidence.py',
    'test_review_requirements.py',
    'test_review_evidence.py',
    'request_policy.py',
    'model_availability.py',
    'test_request_policy.py',
    'test_model_availability.py',
    'test_model_policy_freezing.py',
    'test_model_readiness_integration.py',
    'test_request_secret_redaction.py',
    'host_acceptance.py',
    'host_quality.py',
    'test_host_acceptance.py',
    'test_host_acceptance_config.py',
    'test_host_quality.py',
    'test_host_quality_flow.py',
    'test_host_acceptance_backup.py',
    'session_state.py',
    'test_session_state.py',
    'test_durable_dispatch.py',
    'test_session_crash_recovery.py',
    'test_registered_deepseek_policy.py',
    'test_session_authority.py',
    'test_session_consumers.py',
    'test_repair_scheduling.py',
    'real_task_benchmark.py',
    'test_real_task_benchmark.py',
    'ARCHITECTURE_DIRECTION.md',
    'ARCHITECTURE_PROGRESS.md',
    'dag_scheduler.py',
    'execution_adapters.py',
    'execution_attempts.py',
    'service_lock.py',
    'test_dag_scheduler.py',
    'test_business_scheduling.py',
    'test_execution_adapters.py',
    'test_execution_attempts.py',
    'test_studio_attempts.py',
    'test_attempt_recovery.py',
    'test_service_lock.py',
    'app.py', 'business_routing.py', 'collaboration_comparison.py', 'LICENSE',
    'project_backup.py', 'test_project_backup.py', 'provider_transport.py', 'test_studio_transport.py', 'test_provider_https.py',
    'studio_mcp.py', 'test_studio_mcp.py',
    'mcp_policy.py', 'test_mcp_policy.py',
    'controlled_experiment.py', 'cooperation_metrics.py', 'cooperation_process.py', 'COOPERATION_METHODS.md',
    'role_cooperation.py', 'test_role_cooperation.py',
    'test_controlled_experiment.py', 'test_cooperation_metrics.py', 'test_cooperation_process.py',
    'contribution_ledger.py', 'contribution_tracking.py',
    'collaboration_evaluation.py', 'development_evaluation.py', 'evidence_review.py', 'live.py', 'model_catalog.py',
    'model_registry.py', 'native_checks.py', 'orchestrator.py', 'project_team.py',
    'repair_routing.py', 'test_repair_routing.py', 'test_repair_review_evidence.py', 'test_repair_routing_boundaries.py',
    'quality_assessment.py', 'quality_telemetry.py', 'quality_workflow.py', 'runtime_versions.py',
    'studio.py', 'studio_audit.py', 'studio_audit_report.py', 'studio_business.py',
    'studio_business_demo.py', 'studio_client.py', 'studio_demo.py',
    'studio_native.py', 'studio_native_collection.py', 'task_contracts.py', 'work_policy.py',
    'package_source.py', 'SOURCE_README.md', 'start.command', 'work/reuse_previous_visuals.py',
    'test_app_startup.py', 'test_business_routing.py', 'test_collaboration_comparison.py',
    'test_collaboration_evaluation.py', 'test_development_evaluation.py', 'test_single_model_comparison.py',
    'test_custom_roles.py', 'test_work_framework.py', 'test_live.py',
    'test_contribution_ledger.py', 'test_contribution_tracking.py', 'test_work_policy.py', 'test_deep_review.py',
    'test_model_registry.py', 'test_native_required_checks.py', 'test_evidence_review.py', 'test_runtime_versions.py',
    'test_orchestrator.py', 'test_package_source.py', 'test_project_team.py',
    'test_quality_assessment.py', 'test_quality_flow.py', 'test_quality_telemetry.py',
    'test_studio.py', 'test_context_encoding.py', 'test_child_format_repair.py', 'test_studio_audit.py', 'test_studio_business.py',
    'test_studio_native.py', 'test_studio_native_collection.py',
    'test_studio_response_evidence.py', 'test_task_contracts.py', 'test_workflow.py',
    'test_flow.js', 'test_studio_business_ui.js', 'test_studio_replay.js',
    'test_studio_visual.js',
    'public/app.js',
    'public/flow.js', 'public/index.html', 'public/live.css', 'public/live.html',
    'public/live.js', 'public/motion.js', 'public/style.css',
    'public/studio.html', 'public/studio.js', 'public/studio.css',
    'public/studio-motion.js', 'public/studio-replay.js', 'public/studio-tree.css',
    'public/studio-tree.js', 'public/icons/claude-color.svg',
    'public/icons/deepseek-color.svg', 'public/icons/gemini-color.svg',
    'public/icons/openai.svg', 'public/icons/LICENSE', 'public/icons/SOURCES.txt',
)
BRIDGE_FILES = (
    'lancer_adapter.py', 'bridge.mjs', 'rpc.mjs', 'cli.mjs', 'package.json', 'LICENSE',
    'test/bridge.test.mjs',
)
MCP_FILES = ('adapter.mjs', 'browser.config.json', 'package.json', 'package-lock.json', 'README.md', 'LICENSE',
             'test/adapter.test.mjs', 'test/fixture-server.mjs', 'test/browser.integration.mjs')
RESEARCH_LINKS = (
    '<a href="/research.html" target="_blank" rel="noopener">原理与证据 ↗</a>',
    '<a href="/research.html" title="研究与原理" target="_blank" rel="noopener">ⓘ</a>',
    '<a href="/comments.html" target="_blank" rel="noopener">作者讨论与复刻依据 ↗</a>',
)


def build_archive(destination, include_bridge=True):
    entries = [('README.md', HERE / 'SOURCE_README.md'), ('LICENSE', HERE / 'LICENSE')]
    entries.extend(('lancer-local/' + name, HERE / name) for name in SOURCE_FILES)
    entries.extend(('lancer-mcp-bridge/' + name, HERE.parent / 'lancer-mcp-bridge' / name) for name in MCP_FILES)
    if include_bridge:
        entries.extend(('lancer-codex-bridge/' + name,
                        HERE.parent / 'lancer-codex-bridge' / name)
                       for name in BRIDGE_FILES)
    content = read_sources(entries)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, data in content:
            archive.writestr(PREFIX + '/' + name, data)
        manifest = {'schema': 'yuki-source-preview/v1', 'optionalBridge': include_bridge, 'optionalMcp': True,
                    'files': [{'path': name, 'bytes': len(data),
                               'sha256': hashlib.sha256(data).hexdigest()}
                              for name, data in content]}
        archive.writestr(PREFIX + '/SOURCE_MANIFEST.json',
                         json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    return manifest


def read_sources(entries):
    # No directory traversal: new source files require an explicit manifest edit.
    content = []
    for name, path in entries:
        if path.is_symlink() or not path.is_file():
            raise ValueError('Source is missing or is a symbolic link: ' + name)
        data = path.read_bytes()
        if name in ('lancer-local/public/index.html', 'lancer-local/public/live.html'):
            for link in RESEARCH_LINKS:
                data = data.replace(link.encode('utf8'), b'')
        content.append((name, data))
    return content


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path,
                        default=HERE.parent / 'yuki-source-preview.zip')
    parser.add_argument('--without-bridge', action='store_true',
                        help='Omit the optional native Codex bridge sources')
    args = parser.parse_args()
    manifest = build_archive(args.output, include_bridge=not args.without_bridge)
    print(str(args.output) + ': ' + str(len(manifest['files'])) + ' source files')


if __name__ == '__main__':
    main()
