"""Create an editable starter workflow without starting workers."""
import json
import shutil
from pathlib import Path
from .ports import TeamError, run


def initialize(config_path, repo, model):
    config_path = Path(config_path).expanduser().resolve()
    repo = Path(repo).expanduser().resolve(strict=True)
    if run(['git', 'rev-parse', '--show-toplevel'], repo).strip() != str(repo):
        raise TeamError('repo must be a Git repository root')
    branch = run(['git', 'branch', '--show-current'], repo).strip()
    if not branch:
        raise TeamError('init requires a checked-out base branch')
    run(['git', 'rev-parse', '--verify', branch + '^{commit}'], repo)
    binary = shutil.which('agentctl')
    if not binary:
        raise TeamError('agentctl is not available on PATH')
    if not isinstance(model, str) or not model.strip():
        raise TeamError('model must be nonempty')
    if not config_path.parent.is_dir():
        raise TeamError('config parent directory must already exist')
    directory = config_path.with_name(config_path.stem + '-stages')
    if config_path.exists() or directory.exists():
        raise TeamError('init refuses to overwrite config or stage directory')
    policies = {
        'work': 'Implement the issue, preserving existing behavior. Run relevant checks and commit application changes on this item branch. Report ready, or blocked with the concrete obstacle. Do not modify the root checkout.',
        'review': 'Independently assess the issue and changes. Run relevant checks. Fix concrete in-scope defects and commit fixes, then report changes_made for a fresh review. Report passed if acceptance is met without changes. Do not expand scope for optional improvements. Report blocked if necessary.',
        'integration': 'You are the only integration worker. Read .agentctl-team/root-checkout.txt for the main checkout path. Require it to be clean and on the configured base branch. Merge the base branch into this item branch, preserving delivered features. Resolve conflicts and run relevant checks. Report needs_review if substantive conflict resolution changed code; do not land yet. Otherwise run .agentctl-team/check-runtime-files.sh and merge the item branch into the root checkout. Never stash, reset, force-add ignored files, or discard work. Report complete only after confirming the root checkout is clean. Report blocked on unresolved errors.',
    }
    stages = {}
    try:
        directory.mkdir()
        for name, policy in policies.items():
            files = directory / name
            files.mkdir()
            (files / 'brief.md').write_text('Your role is ' + name + ' for {{ item_id }}.\nRun .agentctl-team/read-issue.sh and read the issue and prior reports.\n' + policy + '\nUse explicit application paths with git add; never commit .agentctl-team files.\nWrite .agentctl-team/report.md and successfully run .agentctl-team/submit-report.sh BEFORE ending. A final chat response is not a report. After submission make no more changes and end your run.\n')
            (files / 'report.md').write_text('---\noutcome: <replace-with-outcome>\n---\n\nReplace this text with findings, completed work, checks, and blockers.\n')
            scripts = {
                'read-issue.sh': '{{ team_command }} --config {{ config_path }} issue show {{ item_id }}',
                'submit-report.sh': '{{ team_command }} --config {{ config_path }} report --attempt {{ attempt }} --file .agentctl-team/report.md',
            }
            if name == 'integration':
                (files / 'root-checkout.txt').write_text(str(repo) + '\nBase branch: ' + branch + '\n')
                scripts['check-runtime-files.sh'] = 'tracked=$(git ls-files -- .agentctl-team)\nif [ -n "$tracked" ]; then\n  echo "ERROR: pipeline runtime files are tracked; preserve them and remove them from the index before landing" >&2\n  echo "$tracked" >&2\n  exit 1\nfi'
            for filename, command in scripts.items():
                path = files / filename
                path.write_text('#!/bin/sh\nset -eu\n' + command + '\n')
                path.chmod(0o755)
            outcomes = {
                'work': {'ready': 'review', 'blocked': 'human_work'},
                'review': {'passed': 'integration', 'changes_made': 'review', 'blocked': 'human_review'},
                'integration': {'complete': 'done', 'needs_review': 'review', 'blocked': 'human_integration'},
            }[name]
            stages[name] = dict(type='llm', pools=['agents', 'integration'] if name == 'integration' else ['agents'],
                                timeout_seconds=1800, worker={'harness': 'codex', 'model': model,
                                'options': {'reasoning_effort': 'low'}}, files=str(files.relative_to(config_path.parent)),
                                outcomes=outcomes, on_error='human_' + name, max=4, overflow='human_' + name)
            stages['human_' + name] = {'type': 'human', 'outcomes': {'retry': name}}
        config = dict(repo=str(repo), base_branch=branch, agentctl=binary,
                      worktrees=str(Path.home() / '.agentctl-team/worktrees' / __import__('hashlib').sha256(str(repo).encode()).hexdigest()[:12]),
                      poll_seconds=2, pools={'agents': 8, 'integration': 1}, entry='work', stages=stages,
                      human_wait_timeout_seconds=1800,
                      launch_message='Read .agentctl-team/brief.md for your role. Run .agentctl-team/read-issue.sh for task context. Update .agentctl-team/report.md and run .agentctl-team/submit-report.sh when finished.')
        with config_path.open('x') as handle:
            json.dump(config, handle, indent=2)
            handle.write('\n')
    except Exception:
        # Leave partial artifacts visible for inspection; never hide a failed init.
        raise
    return {'config': str(config_path), 'stage_files': str(directory), 'base_branch': branch,
            'note': 'Review the generated prompts and limits before start. Human retry does not reset lifetime stage counts.'}
