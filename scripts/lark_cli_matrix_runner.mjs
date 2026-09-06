#!/usr/bin/env node
import { createHmac } from 'node:crypto';
import { execFileSync, spawn, spawnSync } from 'node:child_process';
import {
  appendFileSync,
  closeSync,
  existsSync,
  mkdtempSync,
  mkdirSync,
  openSync,
  readFileSync,
  rmSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname } from 'node:path';
import { io } from '/Users/hermes/code/hermes-web-ui/node_modules/socket.io-client/build/esm/index.js';

const SCENARIOS = {
  t1: {
    label: 'group-webui',
    transport: 'webui',
    profile: 'feishu_group_dfe8bc83167b_e18e',
    openid: 'ou_aaaaaaaaaaaaaaaa0000000000000001',
    name: 'owner',
  },
  t2: {
    label: 'group-feishu',
    transport: 'feishu',
    profile: 'feishu_group_dfe8bc83167b_e18e',
    senderProfile: 'feishu_g41a5b5g',
    openid: 'ou_aaaaaaaaaaaaaaaa0000000000000001',
    chatId: 'oc_cccccccccccccccc0000000000000001',
    botOpenid: 'ou_dddddddddddddddd0000000000000001',
    mentionName: '2号 BOT_A',
    chatType: 'group',
  },
  t3: {
    label: 'owner-webui',
    transport: 'webui',
    profile: 'feishu_g41a5b5g',
    openid: 'ou_aaaaaaaaaaaaaaaa0000000000000001',
    name: 'owner',
  },
  t4: {
    label: 'owner-feishu',
    transport: 'feishu',
    profile: 'feishu_g41a5b5g',
    senderProfile: 'feishu_g41a5b5g',
    openid: 'ou_aaaaaaaaaaaaaaaa0000000000000001',
    chatId: 'oc_bbbbbbbbbbbbbbbb0000000000000001',
    chatType: 'p2p',
  },
};

const CAPABILITIES = {
  approval: { skill: 'lark-approval', service: 'approval', schemaCommand: 'approval instances get --help', canary: '请只执行这一条 lark_cli 命令：`approval instances --help`；不要创建或审批任何实例。只回答可用子命令。' },
  attendance: { skill: 'lark-attendance', service: 'attendance', schemaCommand: 'attendance user_tasks query --help', canary: '执行 `attendance user_tasks query --help` 做只读 canary；不要真实查询员工考勤数据。只回答命令是否可用和核心参数。' },
  base: { skill: 'lark-base', service: 'base', schemaCommand: 'base +base-create --help', canary: '创建一个最小多维表格。请只执行这一条 lark_cli 命令：`base +base-create --name HERMES_MATRIX_BASE_${MARK}`。只回答 app_token/base token 和链接。' },
  calendar: { skill: 'lark-calendar', service: 'calendar', schemaCommand: 'calendar +create --help', canary: '查看今天 agenda。请只执行这一条 lark_cli 命令：`calendar +agenda --format json`。只回答日程数量和前三个标题。' },
  contact: { skill: 'lark-contact', service: 'contact', schemaCommand: 'contact +search-user --help', canary: '搜索用户“owner”。请只执行这一条 lark_cli 命令：`contact +search-user --query "owner" --format json --as user`；不要改成 keyword，不要去掉 +。只回答是否找到、open_id 和姓名。' },
  docs: { skill: 'lark-doc', service: 'docs', schemaCommand: 'docs +create --api-version v2 --help', canary: '创建一个飞书云文档。请只执行这一条 lark_cli 命令：`docs +create --api-version v2 --content "<title>HERMES_MATRIX_DOC_${MARK}</title><p>matrix ${MARK}</p>"`。只回答 document_id 和链接。' },
  drive: { skill: 'lark-drive', service: 'drive', schemaCommand: 'drive +create-folder --help', canary: '请只执行这一条 lark_cli 命令：`drive +search --query HERMES_MATRIX --format json`；只回答命中数量和前三个标题。' },
  event: { skill: 'lark-event', service: 'event', schemaCommand: 'event schema im.message.receive_v1', canary: '执行 event status；只回答是否成功，以及是否出现 identity 参数错误。' },
  im: { skill: 'lark-im', service: 'im', schemaCommand: 'im +messages-send --help', canary: '搜索可见群聊“群聊 P1 测试”；只回答是否找到、chat_id 和群名。' },
  mail: { skill: 'lark-mail', service: 'mail', schemaCommand: 'mail +triage --help', canary: '请只执行这一条 lark_cli 命令：`mail +triage --max 5 --format json`；只回答邮件数量或真实错误。' },
  markdown: { skill: 'lark-markdown', service: 'markdown', schemaCommand: 'markdown +create --help', canary: '创建一个 Markdown 文件。请只执行这一条 lark_cli 命令：`markdown +create --name HERMES_MATRIX_MARKDOWN_${MARK}.md --content "matrix ${MARK}"`。只回答 file_token 和链接。' },
  minutes: { skill: 'lark-minutes', service: 'minutes', schemaCommand: 'minutes +search --help', canary: '请只执行这一条 lark_cli 命令：`minutes +search --query HERMES_MATRIX --format json`；只回答命中数量或真实错误。' },
  okr: { skill: 'lark-okr', service: 'okr', schemaCommand: 'okr +cycle-list --help', canary: '请只执行这一条 lark_cli 命令：`okr +cycle-list --user-id ou_aaaaaaaaaaaaaaaa0000000000000001 --format json`；只回答周期数量或真实错误。' },
  'openapi-explorer': { skill: 'lark-openapi-explorer', service: 'api', helpCommand: 'api GET /open-apis/bot/v3/info --as bot', schemaCommand: 'api GET /open-apis/bot/v3/info --as bot --format json', canary: '请只执行这一条 lark_cli 命令：`api GET /open-apis/bot/v3/info --as bot`；只回答 app_name 和 open_id。' },
  shared: { skill: 'lark-shared', service: 'auth', schemaCommand: 'auth status --help', canary: '检查 lark-cli 当前身份状态或 auth status 帮助；只回答 user/bot 是否可用。' },
  sheets: { skill: 'lark-sheets', service: 'sheets', schemaCommand: 'sheets +create --help', canary: '创建一个飞书电子表格。请只执行这一条 lark_cli 命令：`sheets +create --title HERMES_MATRIX_SHEET_${MARK}`。只回答 spreadsheet_token 和链接。' },
  'skill-maker': { skill: 'lark-skill-maker', service: 'schema', schemaCommand: 'drive +create-folder --help', canary: '说明如何把 drive create-folder 封装成 lark skill，并必须先调用 lark_cli 查看 `drive +create-folder --help`；只回答必要字段。' },
  slides: { skill: 'lark-slides', service: 'slides', schemaCommand: 'slides +create --help', canary: '创建一个飞书幻灯片。请只执行这一条 lark_cli 命令：`slides +create --title HERMES_MATRIX_SLIDES_${MARK}`。只用纯文本回答 presentation_id 和完整 https 链接。' },
  task: { skill: 'lark-task', service: 'task', schemaCommand: 'task +get-my-tasks --help', canary: '请只执行这一条 lark_cli 命令：`task +get-my-tasks --format json`；只回答任务数量和前三个标题或真实错误。' },
  vc: { skill: 'lark-vc', service: 'vc', schemaCommand: 'vc +search --help', canary: '请只执行这一条 lark_cli 命令：`vc +search --start 2026-05-16 --end 2026-05-17 --format json`；只回答数量或真实错误。' },
  'vc-agent': { skill: 'lark-vc-agent', service: 'vc', schemaCommand: 'vc +meeting-join --help', canary: '查看 vc-agent 入会命令帮助。请只执行这一条 lark_cli 命令：`vc +meeting-join --help`；不要真实加入会议。只回答必须参数。' },
  whiteboard: { skill: 'lark-whiteboard', service: 'docs', schemaCommand: 'docs +whiteboard-update --help', canary: '查看 whiteboard 更新命令帮助；不要修改真实画板。只回答支持的输入类型。' },
  wiki: { skill: 'lark-wiki', service: 'wiki', schemaCommand: 'wiki +space-list --help', canary: '请只执行这一条 lark_cli 命令：`wiki +space-list --format json`；只回答数量和前三个名称。' },
  'workflow-meeting-summary': { skill: 'lark-workflow-meeting-summary', service: 'vc', schemaCommand: 'vc +search --help', canary: '按会议纪要汇总工作流，请只执行这一条 lark_cli 命令：`vc +search --start 2026-05-16 --end 2026-05-17 --format json`；只回答数量或真实错误，不生成新文档。' },
  'workflow-standup-report': { skill: 'lark-workflow-standup-report', service: 'calendar', schemaCommand: 'calendar +agenda --help', canary: '按 standup 工作流，请调用 lark_cli 分别执行 `calendar +agenda --format json` 和 `task +get-my-tasks --format json`；只回答两个数量或真实错误。' },
};

function promptsFor(capability) {
  const cap = CAPABILITIES[capability];
  if (!cap) return null;
  const mustUseTool = '请正常说人话，但必须真实调用工具面板里的 lark_cli；不要把命令写成 XML/Markdown 文本；最多调用 lark_cli 两次，失败就停止并如实回答，不要换接口反复试探。';
  const schemaInstruction = cap.schemaCommand
    ? `${mustUseTool} 请只执行这一条 lark_cli 命令：\`${cap.schemaCommand}\`，验证 ${cap.skill} 的参数/帮助。只回答命令是否成功和关键字段。`
    : `${mustUseTool} 请只执行这一条 lark_cli 命令：\`schema ${cap.schema}\`，验证 ${cap.skill} 的参数结构。只回答命令是否成功和必填字段。`;
  const helpCommand = cap.helpCommand || `${cap.service} --help`;
  return [
    `矩阵 ${capability} 1/3：${mustUseTool} 请只执行这一条 lark_cli 命令：\`${helpCommand}\`，验证 ${cap.skill} 能力是否可见。只回答命令是否成功和关键字段。`,
    `矩阵 ${capability} 2/3：${schemaInstruction}`,
    `矩阵 ${capability} 3/3：${mustUseTool} ${cap.canary}`,
  ];
}

function renderPrompt(prompt, mark) {
  const rendered = prompt.replaceAll('${MARK}', mark);
  const withMark = rendered.includes(mark) ? rendered : `${rendered} 测试标记：${mark}。`;
  return `${withMark}\n\n硬性要求：
1. 必须真实调用工具面板里的 lark_cli；如果没有调用 lark_cli，本条测试判失败。
2. 最终回答第一行必须原样输出：测试标记：${mark}
3. 测试标记中的下划线必须保持原样，不要写成反斜杠转义形式。
4. 不得复用历史结果、上一轮结果或模型记忆；必须等本轮 lark_cli 工具返回 stdout/错误后才能作答。
5. 第一行之后再用一两句话说明真实结果。`;
}

function sleepSync(ms) {
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
}

function sqliteJson(dbPath, sql, options = {}) {
  if (!existsSync(dbPath)) return [];
  const timeoutMs = Number(options.timeoutMs ?? 5000);
  const retries = Number(options.retries ?? 4);
  const delayMs = Number(options.delayMs ?? 250);
  for (let attempt = 0; attempt < retries; attempt += 1) {
    try {
      const out = execFileSync(
        'sqlite3',
        ['-cmd', `.timeout ${timeoutMs}`, '-json', dbPath, sql],
        { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] },
      ).trim();
      return out ? JSON.parse(out) : [];
    } catch (err) {
      const msg = `${err.stderr || err.message || ''}`;
      if (!/database is locked|SQLITE_BUSY/i.test(msg)) throw err;
      if (attempt + 1 < retries) sleepSync(delayMs * (attempt + 1));
    }
  }
  return [];
}

function collectStateRows({ profile, mark }) {
  const dbPath = `/Users/hermes/.hermes/profiles/${profile}/state.db`;
  const users = sqliteJson(
    dbPath,
    `select id from messages where role='user' and content like '%${mark}%' order by id desc limit 1;`,
  );
  const userId = Number(users[0]?.id || 0);
  if (!userId) return [];
  const nextUsers = sqliteJson(
    dbPath,
    `select id from messages where role='user' and id > ${userId} order by id asc limit 1;`,
  );
  const nextUserId = Number(nextUsers[0]?.id || 0);
  const upperBound = nextUserId ? ` and id < ${nextUserId}` : '';
  return sqliteJson(
    dbPath,
    `select id, datetime(timestamp,'unixepoch','localtime') as ts, role, coalesce(tool_name,'') as tool_name, coalesce(content,'') as content from messages where id >= ${userId}${upperBound} order by id;`,
  );
}

function logToolRows({ profile, platform, startTs, endTs }) {
  const logPath = `/Users/hermes/.hermes/profiles/${profile}/logs/agent.log`;
  if (!existsSync(logPath) || !startTs) return [];
  const effectiveEnd = endTs || '9999-12-31 23:59:59';
  const marker = `[agent:profile:${profile}:platform:${platform}:`;
  const lines = readFileSync(logPath, 'utf8').split(/\r?\n/);
  return lines
    .filter((line) => {
      const ts = line.slice(0, 19);
      return ts >= startTs && ts <= effectiveEnd && line.includes(marker) && line.includes('tool.started lark_cli');
    })
    .map((line, idx) => ({
      id: `log:${idx}`,
      ts: line.slice(0, 19),
      role: 'tool',
      tool_name: 'lark_cli',
      content: 'tool.started lark_cli from agent.log',
    }));
}

function rowsToTools(rows) {
  return rows
    .filter((row) => String(row.tool_name || '').trim() || row.role === 'tool')
    .map((row) => ({
      event: 'completed',
      name: row.tool_name || 'tool',
      error: /"ok":\s*false|"error"|tool_error/i.test(row.content || ''),
    }));
}

function rowsToOutput(rows, mark = '') {
  const assistantRows = rows
    .filter((row) => row.role === 'assistant' && !row.tool_name && String(row.content || '').trim())
    .map((row) => row.content);
  if (mark) {
    const marked = assistantRows.filter((content) => String(content || '').includes(mark));
    if (marked.length) return marked.at(-1) || '';
    return '';
  }
  return rows
    .filter((row) => row.role === 'assistant' && !row.tool_name && String(row.content || '').trim())
    .map((row) => row.content)
    .at(-1) || '';
}

function outputLooksInterim(output) {
  return /(补上后重试|再次调用|继续重试|重试一次|让我.*重试|让我.*再试|让我先调用工具|补齐参数后|using .* retry)/i.test(String(output || ''));
}

function arg(name, fallback = '') {
  const idx = process.argv.indexOf(`--${name}`);
  return idx >= 0 ? process.argv[idx + 1] : fallback;
}

function pidAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

function acquireRunnerLock(outPath) {
  const lockPath = '/tmp/hermes-lark-cli-matrix/.runner.lock';
  mkdirSync(dirname(lockPath), { recursive: true });
  try {
    const fd = openSync(lockPath, 'wx');
    writeFileSync(fd, JSON.stringify({
      pid: process.pid,
      started_at: new Date().toISOString(),
      out: outPath,
      argv: process.argv.slice(2),
    }, null, 2));
    closeSync(fd);
    return () => {
      try {
        const current = JSON.parse(readFileSync(lockPath, 'utf8'));
        if (Number(current.pid) === process.pid) unlinkSync(lockPath);
      } catch {
        // Best-effort cleanup only.
      }
    };
  } catch (err) {
    if (err.code !== 'EEXIST') throw err;
    let current = {};
    try {
      current = JSON.parse(readFileSync(lockPath, 'utf8'));
    } catch {
      current = {};
    }
    const pid = Number(current.pid || 0);
    if (pid && !pidAlive(pid)) {
      unlinkSync(lockPath);
      return acquireRunnerLock(outPath);
    }
    throw new Error(`another lark-cli matrix runner is active: ${JSON.stringify(current)}`);
  }
}

function envFromPid(pid) {
  const out = execFileSync('ps', ['eww', '-p', String(pid)], { encoding: 'utf8' });
  const env = {};
  for (const part of out.split(/\s+/)) {
    const idx = part.indexOf('=');
    if (idx > 0) env[part.slice(0, idx)] = part.slice(idx + 1);
  }
  return env;
}

function b64url(value) {
  return Buffer.from(value, 'utf8').toString('base64url');
}

function sign(body, secret) {
  return createHmac('sha256', secret).update(body).digest('base64url');
}

function webuiPid() {
  const explicit = Number(arg('webui-pid', '0'));
  if (explicit) return explicit;
  return Number(execFileSync('pgrep', ['-f', 'hermes-web-ui/dist/server/index.js'], { encoding: 'utf8' }).trim().split(/\s+/).at(-1));
}

function makeCookie({ openid, profile, name }) {
  const procEnv = envFromPid(webuiPid());
  const secret = procEnv.FEISHU_SESSION_SECRET || procEnv.FEISHU_APP_SECRET;
  if (!secret) throw new Error('missing FEISHU_SESSION_SECRET from reference WebUI process');
  const now = Math.floor(Date.now() / 1000);
  const body = b64url(JSON.stringify({
    openid,
    profile,
    role: 'user',
    name,
    iat: now,
    exp: now + 3600,
  }));
  return `hermes_feishu_session=${body}.${sign(body, secret)}`;
}

function runOneWebui({ scenarioKey, capability, index, prompt, mark, url, timeoutMs }) {
  const scenario = SCENARIOS[scenarioKey];
  const sessionId = `matrix_${scenarioKey}_${capability}_${index}_${mark}`;
  const renderedPrompt = renderPrompt(prompt, mark);
  console.log(`USER(${scenario.label}/${capability}/${index}): ${renderedPrompt}`);

  return new Promise((resolve) => {
    const socket = io(`${url}/chat-run`, {
      transports: ['websocket'],
      extraHeaders: {
        Cookie: makeCookie(scenario),
      },
      reconnection: false,
      timeout: 10000,
    });
    let final = '';
    const tools = [];
    const startedAt = Date.now();
    let settled = false;
    let pollTimer;
    const pollComplete = () => {
      const rows = collectStateRows({ profile: scenario.profile, mark });
      if (!rows.length) return false;
      const stateTools = rowsToTools(rows);
      const stateOutput = rowsToOutput(rows, mark);
      if (stateTools.length && stateOutput) {
        finish({ status: 'completed', output: stateOutput });
        return true;
      }
      return false;
    };
    const finish = (record) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      clearInterval(pollTimer);
      socket.close();
      const rows = collectStateRows({ profile: scenario.profile, mark });
      const stateTools = rowsToTools(rows);
      const stateOutput = rowsToOutput(rows, mark);
      const recordOutput = String(record.output || '');
      resolve({
        scenario: scenarioKey,
        scenario_label: scenario.label,
        profile: scenario.profile,
        capability,
        message_index: index,
        mark,
        session_id: sessionId,
        prompt: renderedPrompt,
        elapsed_ms: Date.now() - startedAt,
        tools: stateTools.length ? stateTools : tools,
        state_rows: rows.length,
        ...record,
        output: stateOutput || (recordOutput.includes(mark) ? recordOutput : ''),
      });
    };
    const timer = setTimeout(() => finish({ status: 'timeout', output: '' }), timeoutMs);
    pollTimer = setInterval(pollComplete, 3000);

    socket.on('connect', () => {
      socket.emit('run', { input: renderedPrompt, session_id: sessionId, provider: 'zai', model: 'glm-5.1' });
    });
    socket.on('connect_error', (err) => finish({ status: 'connect_error', output: err.message }));
    socket.on('tool.started', (event) => {
      tools.push({ event: 'started', name: event.name || event.tool || '' });
      console.log(`TOOL_START(${scenario.label}/${capability}/${index}): ${event.name || event.tool || ''}`);
    });
    socket.on('tool.completed', (event) => {
      tools.push({ event: 'completed', name: event.name || event.tool || '', error: Boolean(event.error) });
      const out = String(event.output || '').replace(/\s+/g, ' ').slice(0, 240);
      console.log(`TOOL_DONE(${scenario.label}/${capability}/${index}): ${event.name || event.tool || ''} ${out}`);
    });
    socket.on('message.delta', (event) => {
      if (event.delta) final += event.delta;
    });
    socket.on('run.failed', (event) => finish({ status: 'failed', output: event.error || event.output || '' }));
    socket.on('run.completed', (event) => {
      const output = String(event.output || final).trim();
      if (!output.includes(mark) && !rowsToOutput(collectStateRows({ profile: scenario.profile, mark }), mark)) {
        return;
      }
      finish({ status: 'completed', output });
    });
  });
}

function sendFeishuMessage({ scenario, text }) {
  const py = String.raw`
import json, os, subprocess, sys
from pathlib import Path
sys.path.insert(0, "/Users/hermes/code/hermes-multitenancy")
from hermes_multitenancy.webui_broker_server import load_run_broker_shared_env
from hermes_multitenancy.agent_real import _lark_cli_auth_broker_scope

payload = json.loads(sys.argv[1])
load_run_broker_shared_env(Path('/Users/hermes/.hermes'))
profile = Path('/Users/hermes/.hermes/profiles') / payload['senderProfile']
with _lark_cli_auth_broker_scope(profile, payload['openid']) as extra:
    env = {k: v for k, v in os.environ.items() if k in {'PATH','HOME','LANG','LC_ALL','TERM','TMPDIR'}}
    env.update(extra)
    env['HOME'] = str(profile / 'home')
    env['WORKSPACE'] = str(profile / 'workspace')
    env['HERMES_HOME'] = str(profile)
    env['HERMES_PROFILE'] = str(profile)
    body = {'receive_id': payload['chatId'], 'msg_type': 'text', 'content': json.dumps({'text': payload['text']}, ensure_ascii=False)}
    cmd = [extra['HERMES_LARK_CLI_BIN'], 'api', 'POST', '/open-apis/im/v1/messages', '--params', json.dumps({'receive_id_type':'chat_id'}), '--data', json.dumps(body, ensure_ascii=False), '--as', 'user', '--format', 'json']
    p = subprocess.run(cmd, text=True, capture_output=True, env=env, cwd=str(profile / 'workspace'), timeout=60)
    if p.returncode != 0:
        print(json.dumps({'ok': False, 'exit_code': p.returncode, 'stdout': p.stdout, 'stderr': p.stderr}, ensure_ascii=False))
        raise SystemExit(1)
    print(p.stdout)
`;
  const payload = {
    senderProfile: scenario.senderProfile,
    openid: scenario.openid,
    chatId: scenario.chatId,
    text,
  };
  const proc = spawnSync('/Users/hermes/.hermes/hermes-feishu-uat/venv/bin/python', ['-c', py, JSON.stringify(payload)], {
    encoding: 'utf8',
    env: {
      ...process.env,
      PYTHONPATH: '/Users/hermes/code/hermes-multitenancy:/Users/hermes/.hermes/hermes-feishu-uat',
    },
    maxBuffer: 1024 * 1024 * 4,
  });
  if (proc.status !== 0) {
    throw new Error(`send Feishu failed: ${proc.stdout || proc.stderr}`);
  }
  return JSON.parse(proc.stdout);
}

async function sendFeishuMessageWithRecovery({ scenario, text, mark }) {
  let lastError;
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      return { result: sendFeishuMessage({ scenario, text }), recovered: false };
    } catch (err) {
      lastError = err;
      const poll = await pollFeishuResult({ scenario, mark, timeoutMs: 90000 });
      if ((poll.rows || []).length) {
        return { result: {}, recovered: true };
      }
      if (attempt === 0) await sleep(5000);
    }
  }
  throw lastError;
}

async function resetFeishuScenario(scenarioKey) {
  const scenario = SCENARIOS[scenarioKey];
  if (!scenario || scenario.transport !== 'feishu') return;
  const text = scenario.chatType === 'group'
    ? `<at user_id="${scenario.botOpenid}">${scenario.mentionName}</at> /new`
    : '/new';
  console.log(`RESET(${scenario.label}): /new`);
  try {
    sendFeishuMessage({ scenario, text });
    await sleep(5000);
  } catch (err) {
    console.log(`RESET_FAILED(${scenario.label}): ${String(err.message || err).slice(0, 500)}`);
  }
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function pollFeishuResult({ scenario, mark, timeoutMs }) {
  const deadline = Date.now() + timeoutMs;
  let rows = [];
  let lastOutput = '';
  let stableSince = 0;
  while (Date.now() < deadline) {
    rows = collectStateRows({ profile: scenario.profile, mark });
    if (rows.length) {
      const assistantRows = rows.filter((row) => row.role === 'assistant' && !row.tool_name && String(row.content || '').trim());
      const startTs = rows[0]?.ts || '';
      const endTs = assistantRows.at(-1)?.ts || '';
      const mergedRows = rows.concat(logToolRows({ profile: scenario.profile, platform: 'feishu', startTs, endTs }));
      const toolRows = rowsToTools(mergedRows);
      const output = rowsToOutput(rows, mark);
      const latestAssistantTs = assistantRows.at(-1)?.ts || '';
      const latestAgeMs = latestAssistantTs ? Date.now() - new Date(`${latestAssistantTs}+08:00`).getTime() : 0;
      if (output !== lastOutput) {
        lastOutput = output;
        stableSince = Date.now();
      }
      const stableAgeMs = stableSince ? Date.now() - stableSince : 0;
      if (assistantRows.length && toolRows.length && output && !outputLooksInterim(output) && latestAgeMs >= 4000 && stableAgeMs >= 12000) {
        return { status: 'completed', rows };
      }
    }
    await sleep(3000);
  }
  return { status: 'timeout', rows };
}

async function runOneFeishu({ scenarioKey, capability, index, prompt, mark, timeoutMs }) {
  const scenario = SCENARIOS[scenarioKey];
  const renderedPrompt = renderPrompt(prompt, mark);
  const text = scenario.chatType === 'group'
    ? `<at user_id="${scenario.botOpenid}">${scenario.mentionName}</at> ${renderedPrompt}`
    : renderedPrompt;
  console.log(`USER(${scenario.label}/${capability}/${index}): ${renderedPrompt}`);
  const startedAt = Date.now();
  let sendResult;
  try {
    const sent = await sendFeishuMessageWithRecovery({ scenario, text, mark });
    sendResult = sent.result;
  } catch (err) {
    return {
      scenario: scenarioKey,
      scenario_label: scenario.label,
      profile: scenario.profile,
      capability,
      message_index: index,
      mark,
      prompt: renderedPrompt,
      elapsed_ms: Date.now() - startedAt,
      tools: [],
      status: 'send_failed',
      output: String(err.message || err),
    };
  }
  const poll = await pollFeishuResult({ scenario, mark, timeoutMs });
  const stateRows = poll.rows || [];
  const assistantRows = stateRows.filter((row) => row.role === 'assistant' && !row.tool_name && String(row.content || '').trim());
  const rows = stateRows.concat(logToolRows({
    profile: scenario.profile,
    platform: 'feishu',
    startTs: stateRows[0]?.ts || '',
    endTs: assistantRows.at(-1)?.ts || '',
  }));
  const tools = rowsToTools(rows);
  const output = rowsToOutput(rows, mark);
  return {
    scenario: scenarioKey,
    scenario_label: scenario.label,
    profile: scenario.profile,
    capability,
    message_index: index,
    mark,
    prompt: renderedPrompt,
    elapsed_ms: Date.now() - startedAt,
    feishu_message_id: sendResult?.data?.message_id || '',
    tools,
    status: poll.status,
    output,
  };
}

function verdict(record) {
  const output = String(record.output || '');
  const toolUsed = (record.tools || []).some((tool) => String(tool.name || '').includes('lark_cli') || tool.name === 'tool');
  const hasMark = !record.mark || output.includes(record.mark);
  const outputAfterMark = record.mark && output.includes(record.mark)
    ? output.slice(output.indexOf(record.mark) + record.mark.length).replace(/[\s。:：|`*_#-]+/g, '')
    : output.replace(/[\s。:：|`*_#-]+/g, '');
  const informativeOutput = outputAfterMark.length >= 12;
  const blocked = /(permission denied|权限不足|无权访问|无权调用|无权限访问|无权限调用|鉴权错误|Invalid access token|access token.*invalid|not supported.*only supports|only supports:\s*user|不支持.*bot|不支持.*机器人|仅支持.*user|仅支持.*用户|只支持.*user|缺少.*scope|missing required scope|credential unavailable|credentials are provided externally|credentials 由外部提供|do not support interactive management|不支持交互式.*auth|config bind|未绑定.*context|用户身份令牌.*无法|未开通|灰度|ErrNotInGray|无法返回|无法完成|not authorized|forbidden|返回 403|HTTP 403|target host not allowed|错误码|all invalid|user not found|尚未关联邮箱|未关联邮箱|未配置邮箱|邮箱账户)/i.test(output);
  const bad = /(lark-cli 工具不可见|当前 profile 未暴露|command not found|not configured|没有安装|unknown command|unknown flag|未知 flag|命令\s*\*\*未成功|命令未成功|命令失败|执行失败|调用失败|真实错误|exit_code\s*[=:：]?\s*[1-9]|"ok"\s*:\s*false|未能拿到|无法获取|未返回.*数据|两次调用.*失败|均已失败|未成功找到)/i.test(output);
  if (record.status === 'completed' && toolUsed && hasMark && blocked) return 'blocked';
  const artifactPatterns = {
    base: /((app_token|base token|base_token|多维表格 token|bitable).*https?:\/\/\S+|https?:\/\/\S+\/base\/[A-Za-z0-9]+)/is,
    docs: /(document_id|Document ID|document token|文档 ID).*https?:\/\/\S+/is,
    markdown: /((file_token|document_id|文档 ID|文件 token).*https?:\/\/\S+|(文件已创建|创建成功).*https?:\/\/\S+\/file\/[A-Za-z0-9]+)/is,
    sheets: /((spreadsheet_token|spreadsheet token|表格 token).*https?:\/\/\S+|https?:\/\/\S+\/sheets\/[A-Za-z0-9]+)/is,
    slides: /((presentation_id|presentation token|幻灯片 ID|slides token).*https?:\/\/\S+|https?:\/\/\S+\/slides\/[A-Za-z0-9]+)/is,
  };
  const requiredArtifact = record.message_index === 3 ? artifactPatterns[record.capability] : null;
  const missingRequiredArtifact = requiredArtifact && !requiredArtifact.test(output);
  if (record.status === 'completed' && toolUsed && hasMark && missingRequiredArtifact) return 'fail';
  if (record.status === 'completed' && toolUsed && hasMark && outputLooksInterim(output)) return 'fail';
  if (record.status === 'completed' && toolUsed && hasMark && informativeOutput && !bad && !blocked) return 'pass';
  return 'fail';
}

async function main() {
  if (arg('self-test')) {
    const selfTest = arg('self-test');
    if (selfTest === 'calendar-prompt') {
      const prompt = promptsFor('calendar')[2];
      if (!prompt.includes('calendar +agenda --format json')) {
        throw new Error(`calendar canary prompt does not use official +agenda shortcut: ${prompt}`);
      }
      console.log('calendar-prompt ok');
      return;
    }
    if (selfTest === 'calendar-unavailable-verdict') {
      const result = verdict({
        status: 'completed',
        tools: [{ name: 'lark_cli' }],
        mark: 'MARK',
        output: '未能拿到实际日程数据。测试标记：MARK',
      });
      if (result === 'pass') throw new Error('unavailable calendar output was marked pass');
      console.log('calendar-unavailable-verdict ok');
      return;
    }
    if (selfTest === 'prompt-canaries') {
      const expected = {
        contact: 'contact +search-user --query "owner" --format json',
        mail: 'mail +triage --max 5 --format json',
        markdown: 'markdown +create --name HERMES_MATRIX_MARKDOWN_${MARK}.md',
        minutes: 'minutes +search --query HERMES_MATRIX --format json',
        okr: 'okr +cycle-list --user-id ou_aaaaaaaaaaaaaaaa0000000000000001 --format json',
        task: 'task +get-my-tasks --format json',
        'vc-agent': 'vc +meeting-join --help',
        wiki: 'wiki +space-list --format json',
        slides: 'slides +create --title HERMES_MATRIX_SLIDES_${MARK}',
        'workflow-standup-report': 'task +get-my-tasks --format json',
      };
      for (const [capability, needle] of Object.entries(expected)) {
        const text = promptsFor(capability).join('\n');
        if (!text.includes(needle)) throw new Error(`${capability} prompt missing ${needle}`);
      }
      console.log('prompt-canaries ok');
      return;
    }
    if (selfTest === 'no-error-success-verdict') {
      const result = verdict({
        status: 'completed',
        tools: [{ name: 'lark_cli' }],
        mark: 'MARK',
        output: '测试标记：MARK\n\n命令执行成功，调用无报错。',
      });
      if (result !== 'pass') throw new Error(`success text with 无报错 marked ${result}`);
      console.log('no-error-success-verdict ok');
      return;
    }
    if (selfTest === 'truncated-output-verdict') {
      const result = verdict({
        status: 'completed',
        tools: [{ name: 'lark_cli' }],
        mark: 'MARK',
        output: '测试标记：MARK\n\n命令',
      });
      if (result !== 'fail') throw new Error(`truncated output marked ${result}`);
      console.log('truncated-output-verdict ok');
      return;
    }
    if (selfTest === 'mail-user-not-found-blocked') {
      const result = verdict({
        status: 'completed',
        tools: [{ name: 'lark_cli' }],
        mark: 'MARK',
        output: '测试标记：MARK\n\n命令失败（exit_code=1）。错误为 [4013] user not found，当前身份尚未关联邮箱账户。',
      });
      if (result !== 'blocked') throw new Error(`mail user not found marked ${result}`);
      console.log('mail-user-not-found-blocked ok');
      return;
    }
    if (selfTest === 'external-auth-management-blocked') {
      const result = verdict({
        status: 'completed',
        tools: [{ name: 'lark_cli' }],
        mark: 'MARK',
        output: '测试标记：MARK\n\n命令执行失败（exit_code=2）。"auth" is not supported: credentials are provided externally and do not support interactive management。',
      });
      if (result !== 'blocked') throw new Error(`external auth management marked ${result}`);
      console.log('external-auth-management-blocked ok');
      return;
    }
    if (selfTest === 'base-url-artifact') {
      const result = verdict({
        status: 'completed',
        tools: [{ name: 'lark_cli' }],
        mark: 'MARK',
        capability: 'base',
        message_index: 3,
        output: '测试标记：MARK 创建成功。链接：https://example.feishu.cn/base/MT31bPKMpa4XvYsF4GeccP8fnJc',
      });
      if (result !== 'pass') throw new Error(`base URL artifact marked ${result}`);
      console.log('base-url-artifact ok');
      return;
    }
    if (selfTest === 'markdown-link-artifact') {
      const result = verdict({
        status: 'completed',
        tools: [{ name: 'lark_cli' }],
        mark: 'MARK',
        capability: 'markdown',
        message_index: 3,
        output: '测试标记：MARK 文件已创建，链接：https://www.feishu.cn/file/ZuBGbwUMvoJT16xGJRHciRdmnzd。file_token 被脱敏隐藏。',
      });
      if (result !== 'pass') throw new Error(`markdown link artifact marked ${result}`);
      console.log('markdown-link-artifact ok');
      return;
    }
    if (selfTest === 'openapi-schema-prompt') {
      const text = promptsFor('openapi-explorer')[1];
      if (!text.includes('api GET /open-apis/bot/v3/info --as bot --format json')) {
        throw new Error(`openapi schema prompt should use official raw api form: ${text}`);
      }
      if (text.includes('api --help')) throw new Error(`openapi schema prompt used unsupported api --help: ${text}`);
      console.log('openapi-schema-prompt ok');
      return;
    }
    if (selfTest === 'render-prompt-no-cache') {
      const text = renderPrompt('执行 `auth --help`。', 'MARK');
      if (!text.includes('不得复用历史结果')) throw new Error(`rendered prompt missing no-cache guard: ${text}`);
      if (!text.includes('必须等本轮 lark_cli 工具返回')) throw new Error(`rendered prompt missing tool-return guard: ${text}`);
      if (!text.includes('下划线必须保持原样')) throw new Error(`rendered prompt missing underscore guard: ${text}`);
      console.log('render-prompt-no-cache ok');
      return;
    }
    if (selfTest === 'feishu-url-artifacts') {
      const sheet = verdict({
        status: 'completed',
        tools: [{ name: 'lark_cli' }],
        mark: 'MARK',
        capability: 'sheets',
        message_index: 3,
        output: '测试标记：MARK\n\n命令成功，链接为：https://example.feishu.cn/sheets/SFnCsqx2whscuytd',
      });
      const slide = verdict({
        status: 'completed',
        tools: [{ name: 'lark_cli' }],
        mark: 'MARK',
        capability: 'slides',
        message_index: 3,
        output: '测试标记：MARK\n\n命令成功，presentation_id 为 `RkFJ`，链接为：https://example.feishu.cn/slides/RkFJsXdqZlzTVcd46RVchSMdnEg',
      });
      if (sheet !== 'pass' || slide !== 'pass') throw new Error(`feishu URL artifacts marked ${sheet}/${slide}`);
      console.log('feishu-url-artifacts ok');
      return;
    }
    if (selfTest === 'user-not-found-blocked') {
      const result = verdict({
        status: 'completed',
        tools: [{ name: 'lark_cli' }],
        mark: 'MARK',
        output: '测试标记：MARK\n\n命令失败（exit_code=1）。错误为 [4013] user not found，当前身份尚未关联邮箱账户。',
      });
      if (result !== 'blocked') throw new Error(`user not found marked ${result}`);
      console.log('user-not-found-blocked ok');
      return;
    }
    if (selfTest === 'external-credentials-blocked') {
      const result = verdict({
        status: 'completed',
        tools: [{ name: 'lark_cli' }],
        mark: 'MARK',
        output: '测试标记：MARK\n\n命令执行失败（exit_code=2）。"auth" is not supported: credentials are provided externally and do not support interactive management.',
      });
      if (result !== 'blocked') throw new Error(`external credentials auth marked ${result}`);
      console.log('external-credentials-blocked ok');
      return;
    }
    if (selfTest === 'interim-tool-intent') {
      const result = verdict({
        status: 'completed',
        tools: [{ name: 'lark_cli' }],
        mark: 'MARK',
        output: '测试标记：MARK\n\n让我先调用工具查看实际结果。',
      });
      if (result !== 'fail') throw new Error(`interim output marked ${result}`);
      console.log('interim-tool-intent ok');
      return;
    }
    if (selfTest !== 'sqlite-lock') throw new Error(`unknown self-test: ${selfTest}`);
    const dir = mkdtempSync(`${tmpdir()}/hermes-lark-cli-matrix-`);
    const dbPath = `${dir}/locked.db`;
    try {
      execFileSync('sqlite3', [dbPath, 'create table messages(id integer primary key, role text); insert into messages(role) values ("user");']);
      const locker = spawn('sqlite3', [dbPath], { stdio: ['pipe', 'pipe', 'pipe'] });
      locker.stdin.write('begin exclusive;\n');
      locker.stdin.write('update messages set role="locked" where id=1;\n');
      await sleep(100);
      const rows = sqliteJson(dbPath, 'select * from messages;', { timeoutMs: 50, retries: 2, delayMs: 10 });
      locker.kill('SIGTERM');
      if (!Array.isArray(rows)) throw new Error('sqliteJson did not return an array');
      console.log('sqlite-lock ok');
      return;
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  }

  const scenarioKeys = arg('scenarios', 't1,t2,t3,t4').split(',').map((s) => s.trim()).filter(Boolean);
  const capabilities = arg('capabilities', Object.keys(CAPABILITIES).join(',')).split(',').map((s) => s.trim()).filter(Boolean);
  const indices = new Set(arg('indices', '1,2,3').split(',').map((s) => Number(s.trim())).filter((n) => n >= 1 && n <= 3));
  const url = arg('url', 'http://127.0.0.1:8648');
  const timeoutMs = Number(arg('timeout-ms', '180000'));
  const outPath = arg('out', `/tmp/hermes-lark-cli-matrix/${new Date().toISOString().replace(/[:.]/g, '-')}.jsonl`);
  const listOnly = process.argv.includes('--list');
  const resetFeishu = process.argv.includes('--reset-feishu-session');
  mkdirSync(dirname(outPath), { recursive: true });
  const releaseLock = listOnly ? () => {} : acquireRunnerLock(outPath);

  try {
    if (listOnly) {
      for (const capability of capabilities) {
        const prompts = promptsFor(capability);
        if (!prompts) throw new Error(`unknown capability or no prompts yet: ${capability}`);
        console.log(`### ${capability}`);
        prompts.forEach((prompt, idx) => console.log(`${idx + 1}. ${prompt}`));
      }
      return;
    }

    for (const scenarioKey of scenarioKeys) {
      if (!SCENARIOS[scenarioKey]) throw new Error(`unknown scenario: ${scenarioKey}`);
      if (resetFeishu) await resetFeishuScenario(scenarioKey);
      for (const capability of capabilities) {
        const prompts = promptsFor(capability);
        if (!prompts) throw new Error(`unknown capability or no prompts yet: ${capability}`);
        for (let i = 0; i < prompts.length; i += 1) {
          if (!indices.has(i + 1)) continue;
          const mark = `${capability.toUpperCase()}_${scenarioKey.toUpperCase()}_${i + 1}_${Date.now()}`;
          const scenario = SCENARIOS[scenarioKey];
          const record = scenario.transport === 'feishu' ? await runOneFeishu({
            scenarioKey,
            capability,
            index: i + 1,
            prompt: prompts[i],
            mark,
            timeoutMs,
          }) : await runOneWebui({
            scenarioKey,
            capability,
            index: i + 1,
            prompt: prompts[i],
            mark,
            url,
            timeoutMs,
          });
          record.verdict = verdict(record);
          appendFileSync(outPath, `${JSON.stringify(record)}\n`, 'utf8');
          console.log(`RESULT(${record.scenario_label}/${capability}/${i + 1}): ${record.verdict}/${record.status} ${record.output.replace(/\s+/g, ' ').slice(0, 500)}`);
        }
      }
    }
    console.log(`WROTE ${outPath}`);
  } finally {
    releaseLock();
  }
}

main().catch((err) => {
  console.error(err.stack || err.message);
  process.exit(1);
});
