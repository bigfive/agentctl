#!/usr/bin/env python3
"""Prepare a disposable repository and explicit three-stage poem workflow."""
import argparse
import json
import shlex
import shutil
import subprocess
from pathlib import Path


def create(destination):
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    repo = destination / 'repo'
    repo.mkdir()
    def git(*args):
        subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True)
    git('init', '-b', 'main')
    git('config', 'user.name', 'Poem Pipeline')
    git('config', 'user.email', 'poems@example.invalid')
    (repo / 'README.md').write_text('Toy poetry collection. Each poem has exactly four nonempty lines.\n')
    git('add', '.')
    git('commit', '-m', 'Initialize poetry collection')
    agentctl = shutil.which('agentctl')
    if not agentctl:
        raise RuntimeError('agentctl is unavailable')
    agentctl = str(Path(agentctl).resolve())
    stages = {}
    for name, outcomes, instructions in (
        ('write', {'ready': 'review', 'blocked': 'hold'},
         'Write poems/{{ item_id }}.md following the live issue. It must contain exactly four nonempty lines, '
         'without a title or blank lines. Do not commit: the next stage must receive your uncommitted file. '
         'Report ready once written, or blocked if you need an answer.'),
        ('review', {'passed': 'integration', 'changes_made': 'review', 'blocked': 'hold'},
         'Independently review poems/{{ item_id }}.md against the live issue. Confirm exactly four nonempty lines. '
         'Fix defects yourself. If you changed the poem, report changes_made for a new reviewer; '
         'otherwise report passed. Do not commit. Report blocked if you cannot finish.'),
        ('integration', {'complete': 'done', 'blocked': 'hold'},
         'Check poems/{{ item_id }}.md has exactly four nonempty lines. Commit that file on the item branch. '
         'Then integrate it into the toy main checkout using:\n'
         'git -C ' + shlex.quote(str(repo)) + ' merge --no-edit {{ branch }}\n'
         'You are the only integration worker. Resolve any conflicts. Confirm the poem is present in the '
         'toy main checkout. Report complete after success, or blocked if integration fails.'),
    ):
        directory = destination / 'stages' / name
        directory.mkdir(parents=True)
        (directory / 'brief.md').write_text(
            'Your role is ' + name + ' for item {{ item_id }}.\n\n' + instructions + '\n\n'
            'Run .agentctl-team/read-issue.sh for current task details.\n'
            'Write your outcome and findings in .agentctl-team/report.md.\n'
            'Run .agentctl-team/submit-report.sh. If it fails, report the error and stop.\n'
            'After successful submission, make no further changes and end your run.\n')
        (directory / 'report.md').write_text(
            '---\noutcome: <replace-with-outcome>\n---\n\n'
            'Replace this text with your findings, work completed, and any blockers.\n')
        scripts = {
            'read-issue.sh': '{{ team_command }} --config {{ config_path }} issue show {{ item_id }}',
            'submit-report.sh': '{{ team_command }} --config {{ config_path }} report '
                                '--attempt {{ attempt }} --file .agentctl-team/report.md',
        }
        for filename, command in scripts.items():
            script = directory / filename
            script.write_text('#!/bin/sh\nset -eu\n' + command + '\n')
            script.chmod(0o755)
        stages[name] = dict(pools=['agents', 'integration'] if name == 'integration' else ['agents'],
                            timeout_seconds=300, worker={'harness': 'codex', 'model': None},
                            files=str(directory), outcomes=outcomes)
    config = dict(repo=str(repo), base_branch='main', agentctl=agentctl,
                  worktrees=str(destination / 'worktrees'), poll_seconds=1,
                  pools={'agents': 2, 'integration': 1}, stages=stages, entry='write',
                  launch_message='You are working in a worktree prepared with files and scripts.\n\n'
                  'Read .agentctl-team/brief.md to find out your role.\n'
                  'Run .agentctl-team/read-issue.sh to read the task context.\n'
                  'Update .agentctl-team/report.md with your results.\n'
                  'Run .agentctl-team/submit-report.sh once finished.\n')
    config_path = destination / 'pipeline.json'
    config_path.write_text(json.dumps(config, indent=2))
    for item, topic in [('rain', 'rain falling on a quiet city'), ('ocean', 'the ocean at sunrise')]:
        (destination / (item + '.md')).write_text('Write a four-line poem about ' + topic + '.\n')
    return config_path


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('destination', help='new disposable directory')
    print(create(parser.parse_args().destination))
