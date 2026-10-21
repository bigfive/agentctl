#!/usr/bin/env python3
"""Luna-only human handoff plus bounded-loop demonstration."""
import argparse
import importlib.util
import json
from pathlib import Path


def create(destination):
    source = Path(__file__).parents[1] / 'poems/create.py'
    spec = importlib.util.spec_from_file_location('poem_example', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config_path = module.create(destination)
    root = config_path.parent
    config = json.loads(config_path.read_text())
    config['pools']['agents'] = 2
    config['stages']['manager'] = {'type': 'human', 'outcomes': {'clarified': 'review'}}
    config['stages']['escalation'] = {'type': 'human', 'outcomes': {'acknowledged': 'done'}}
    config['stages']['review'].update(max=2, overflow='escalation')
    config['stages']['review']['outcomes']['blocked'] = 'manager'
    for stage in ('write', 'integration'):
        config['stages'][stage]['outcomes']['blocked'] = 'manager'
    review = root / 'stages/review/brief.md'
    review.write_text(
        'You are reviewing {{ item_id }}. Run .agentctl-team/read-issue.sh and inspect the reports.\n'
        'If there is no manager-stage report, report blocked: the title policy needs human clarification.\n'
        'If a manager report exists, read and follow it, inspect poems/{{ item_id }}.md, '
        'and confirm exactly four nonempty lines without a title. Report passed if correct. '
        'Fix any defect and report changes_made if you changed the poem. Do not commit.\n'
        'Write .agentctl-team/report.md, run .agentctl-team/submit-report.sh, then stop.\n')
    loop_files = root / 'stages/loop'
    loop_files.mkdir()
    for name in ('read-issue.sh', 'submit-report.sh', 'report.md'):
        original = root / 'stages/write' / name
        target = loop_files / name
        target.write_text(original.read_text())
        target.chmod(original.stat().st_mode & 0o777)
    (loop_files / 'brief.md').write_text(
        'This is an intentional loop-limit experiment, not a coding task.\n'
        'Run .agentctl-team/read-issue.sh. Report outcome repeat in .agentctl-team/report.md '
        'with a note that this stage requests another execution.\n'
        'Run .agentctl-team/submit-report.sh and stop. Do not change code.\n')
    config['stages']['loop'] = dict(type='llm', pools=['agents'], timeout_seconds=180,
        worker={'harness': 'codex', 'model': 'gpt-6-luna', 'options': {'reasoning_effort': 'low'}},
        files=str(loop_files), outcomes={'repeat': 'loop'}, max=1, overflow='escalation')
    for stage in config['stages'].values():
        if stage.get('type', 'llm') == 'llm':
            stage['worker'] = {'harness': 'codex', 'model': 'gpt-6-luna',
                               'options': {'reasoning_effort': 'low'}}
    config_path.write_text(json.dumps(config, indent=2))
    overflow = dict(config, entry='loop')
    (root / 'overflow-pipeline.json').write_text(json.dumps(overflow, indent=2))
    (root / 'manager-report.md').write_text(
        '---\noutcome: clarified\n---\n\nNo title is required. Keep exactly four nonempty poem lines.\n')
    (root / 'overflow-report.md').write_text(
        '---\noutcome: acknowledged\n---\n\nThe loop limit was reached as expected; end this test item.\n')
    return config_path


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('destination')
    args = parser.parse_args()
    print(create(args.destination))
