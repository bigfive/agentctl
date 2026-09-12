import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { tool } from '@opencode-ai/plugin'

const execFileAsync = promisify(execFile)
const DEFAULT_MAX_CALLBACK_CHARS = 8_000

function json(value) {
  return JSON.stringify(value, null, 2)
}

async function runAgentctl(args, { allowFailure = false } = {}) {
  const command = process.env.AGENTCTL_BIN || 'agentctl'
  try {
    const result = await execFileAsync(command, args, {
      env: process.env,
      maxBuffer: 2 * 1024 * 1024,
    })
    return { ...result, code: 0 }
  } catch (error) {
    if (!allowFailure) throw new Error(error?.stderr?.trim() || error?.message || String(error), { cause: error })
    return {
      stdout: error?.stdout ?? '',
      stderr: error?.stderr ?? '',
      code: error?.code ?? 1,
    }
  }
}

function parseJson(result, operation) {
  try {
    return JSON.parse(result.stdout)
  } catch (error) {
    throw new Error(`agentctl ${operation} returned invalid JSON: ${result.stdout || result.stderr}`, { cause: error })
  }
}

function callbackMessage(record, job, result) {
  const output = String(result?.result ?? '').trim()
  const bounded = output.length > DEFAULT_MAX_CALLBACK_CHARS
    ? `${output.slice(0, DEFAULT_MAX_CALLBACK_CHARS)}\n…[truncated]`
    : output
  return [
    '[agentctl completion]',
    `job: ${job}`,
    `harness: ${record?.harness ?? 'unknown'}`,
    `status: ${record?.status ?? 'unknown'}`,
    bounded ? `result:\n${bounded}` : 'result: (no result recorded)',
    `full: agentctl read ${job}`,
  ].join('\n')
}

function callbackMessageId(job) {
  return `msg_agentctl_${String(job).replace(/[^A-Za-z0-9_-]/g, '_')}`
}

async function notifyCompletion({ client, sessionID, directory, job }) {
  try {
    const waited = await runAgentctl(['wait', job, '--json'], { allowFailure: true })
    const payload = parseJson(waited, 'wait')
    const record = payload.jobs?.[0]
    if (!record) throw new Error(`agentctl wait returned no record for ${job}`)
    await client.session.promptAsync({
      path: { id: sessionID },
      query: { directory },
      body: {
        messageID: callbackMessageId(job),
        parts: [{ type: 'text', text: callbackMessage(record, job, record) }],
      },
    })
  } catch (error) {
    // The durable job remains readable even if the OpenCode server is gone.
    console.error(`[agentctl] callback failed for ${job}: ${error?.message ?? String(error)}`)
  }
}

function contextDirectory(input, context) {
  return input.cwd || context?.directory || context?.worktree || process.cwd()
}

function startArgs(input, context) {
  const cwd = contextDirectory(input, context)
  const args = ['start', input.harness, input.prompt, '--cwd', cwd, '--json']
  if (input.model) args.push('--model', input.model)
  return args
}

function requiredJob(input) {
  const job = String(input.job || '').trim()
  if (!job) throw new Error('agentctl requires a job for this action')
  return job
}

export default async function AgentctlPlugin({ client }) {
  return {
    tool: {
      agentctl: tool({
        description: 'Start and manage durable work in another agent harness. Start and followup return immediately; the originating OpenCode session receives a completion callback when the job finishes.',
        args: {
          action: tool.schema.enum(['start', 'status', 'read', 'followup', 'cancel', 'list']).describe('The agentctl operation to perform.'),
          harness: tool.schema.enum(['codex', 'claude', 'cursor', 'opencode']).optional().describe('Child harness for start.'),
          prompt: tool.schema.string().optional().describe('Instructions for start or followup.'),
          job: tool.schema.string().optional().describe('agentctl job handle for status, read, followup, or cancel.'),
          cwd: tool.schema.string().optional().describe('Absolute project directory for start.'),
          model: tool.schema.string().optional().describe('Optional child model override.'),
        },
        async execute(input, context) {
          const sessionID = context?.sessionID
          const directory = contextDirectory(input, context)
          if (!sessionID) throw new Error('agentctl OpenCode plugin requires a session context')

          if (input.action === 'start') {
            if (!input.harness || !input.prompt?.trim()) throw new Error('agentctl start requires harness and prompt')
            const record = parseJson(await runAgentctl(startArgs(input, context)), 'start')
            void notifyCompletion({ client, sessionID, directory, job: record.job })
            return json({ job: record.job, status: record.status, callback: 'scheduled' })
          }

          if (input.action === 'followup') {
            const job = requiredJob(input)
            if (!input.prompt?.trim()) throw new Error('agentctl followup requires prompt')
            const child = parseJson(await runAgentctl(['followup', job, input.prompt, '--json']), 'followup')
            void notifyCompletion({ client, sessionID, directory, job: child.job })
            return json({ job: child.job, followupOf: job, status: child.status, callback: 'scheduled' })
          }

          if (input.action === 'status') return (await runAgentctl(['status', requiredJob(input), '--json'])).stdout
          if (input.action === 'read') return (await runAgentctl(['read', requiredJob(input)])).stdout
          if (input.action === 'cancel') return (await runAgentctl(['cancel', requiredJob(input), '--json'])).stdout
          if (input.action === 'list') return (await runAgentctl(['list', '--json'])).stdout
          throw new Error(`Unsupported agentctl action: ${input.action}`)
        },
      }),
    },
  }
}
