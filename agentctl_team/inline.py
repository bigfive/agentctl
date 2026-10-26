"""Prepare the report tools used by stages with inline prompts."""


def stage_files(stage):
    brief = (stage['prompt'] + '\n\n'
             'Run .agentctl-team/read-issue.sh to read the task and prior reports.\n'
             'Write your findings in .agentctl-team/report.md with front matter '
             'containing exactly outcome: NAME. Choose one of: ' +
             ', '.join(stage['outcomes']) + '.\n'
             'Run .agentctl-team/submit-report.sh successfully, then stop making '
             'changes and end your run.\n')
    return {
        'brief.md': (brief, 0o644),
        'report.md': ('---\noutcome: <replace-with-outcome>\n---\n\n'
                      'Replace this text with findings, completed work, and blockers.\n', 0o644),
        'read-issue.sh': ('#!/bin/sh\nset -eu\n'
                          '{{ team_command }} --config {{ config_path }} issue show {{ item_id }}\n', 0o755),
        'submit-report.sh': ('#!/bin/sh\nset -eu\n'
                             '{{ team_command }} --config {{ config_path }} report '
                             '--attempt {{ attempt }} --file .agentctl-team/report.md\n', 0o755),
    }
