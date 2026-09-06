import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import vm from 'node:vm'
import { EventEmitter } from 'node:events'
import { fileURLToPath } from 'node:url'

const sourcePath = fileURLToPath(new URL('../bridge/bridge.js', import.meta.url))
const source = fs.readFileSync(sourcePath, 'utf8')
const marker = '// Bridge runtime. Tests execute this unchanged body with synthetic transports.'
assert.equal(source.split(marker).length, 2)
const body = source.slice(source.indexOf(marker))

class Exit extends Error { constructor (status) { super('process exited'); this.status = status } }

async function harness (t, { command = 'daemon', paired = true, args = [], before, missingStore = false } = {}) {
  const store = fs.mkdtempSync(path.join(os.tmpdir(), 'whatsapp-bridge-test-'))
  t.after(() => fs.rmSync(store, { recursive: true, force: true }))
  if (before) before(store)
  const process = new EventEmitter()
  process.env = missingStore ? {} : { WA_BRIDGE_DIR: store }
  process.argv = ['node', sourcePath, command, ...args]
  process.pid = 12345
  process.kill = () => { throw new Error('no process') }
  process.exit = (status) => { process.emit('exit', status); throw new Exit(status) }
  const timers = []
  const logs = []
  let handler, options, calls = 0
  const P = () => ({ warn: () => {} })
  P.destination = () => 2
  const context = {
    fs, path, process, console: { log: (...v) => logs.push(v), error: (...v) => logs.push(v) },
    P, QRCode: { toString: async () => 'SYNTHETIC_QR' },
    setTimeout: (fn, delay) => { const timer = { fn, delay, unref () { return this } }; timers.push(timer); return timer },
    clearTimeout: () => {},
    useMultiFileAuthState: async () => ({ state: { creds: paired ? { me: { id: 'example@lid' } } : {}, keys: {} }, saveCreds: async () => {} }),
    makeCacheableSignalKeyStore: (keys) => keys,
    fetchLatestBaileysVersion: async () => ({ version: [1, 2, 3] }),
    DisconnectReason: { loggedOut: 401, connectionReplaced: 440, restartRequired: 515 },
    Browsers: { windows: (name) => ['Windows', name, '1'] },
    extractMessageContent: (value) => value,
    getContentType: (value) => Object.keys(value)[0],
    toNumber: (value) => Number(value),
    makeWASocket: (settings) => {
      options = settings
      calls++
      // No send, read-receipt, or group-mutation methods exist on the transport.
      return { ev: { process: (fn) => { handler = fn } },
               groupFetchAllParticipating: async () => ({}), end: () => {} }
    }
  }
  vm.createContext(context)
  // Capture start's returned promise so tests also assert authentication failures.
  vm.runInContext(body.replace(/\n    start\('(daemon|drain)'/g, "\n    globalThis.started = start('$1'"), context, { filename: sourcePath })
  if (context.started) await context.started
  return { store, process, timers, logs, get options () { return options }, get calls () { return calls },
           emit: (event) => handler(event), rows: () => {
             const dir = path.join(store, 'spool')
             return fs.existsSync(dir) ? fs.readdirSync(dir).flatMap(file => fs.readFileSync(path.join(dir, file), 'utf8').trim().split('\n').filter(Boolean).map(JSON.parse)) : []
           } }
}

function message (id, content = { conversation: 'Example message' }) {
  return { key: { remoteJid: 'example@g.us', participant: 'sender@lid', id, fromMe: false },
           messageTimestamp: 1767225600, pushName: 'Example sender', message: content }
}

test('live, offline queue, and history retain text, metadata, and source without send methods', async t => {
  const h = await harness(t)
  await h.emit({ 'messages.upsert': { type: 'notify', messages: [message('live')] } })
  await h.emit({ 'messages.upsert': { type: 'append', messages: [message('offline')] } })
  await h.emit({ 'messaging-history.set': { messages: [message('history')], chats: [{ id: 'example@g.us', name: 'Example group' }], contacts: [] } })
  await h.emit({ 'messages.upsert': { messages: [message('image', { imageMessage: { caption: 'Example caption', mimetype: 'image/png' } })] } })
  assert.deepEqual(h.rows().map(row => row.msg_id), ['live', 'offline', 'history', 'image'])
  assert.deepEqual(h.rows().map(row => row.source), ['live', 'live', 'history', 'live'])
  assert.equal(h.rows()[3].media.caption, 'Example caption')
  assert.equal(h.rows()[3].sender_name, 'Example sender')
  assert.equal(h.options.syncFullHistory, true)
  assert.equal(h.options.markOnlineOnConnect, false)
  assert.equal(h.options.shouldSyncHistoryMessage(), true)
  assert.deepEqual(Array.from(h.options.browser), ['Windows', 'Desktop', '1'])
})

test('broadcasts and unsupported system messages are excluded', async t => {
  const h = await harness(t)
  await h.emit({ 'messages.upsert': { messages: [
    { ...message('broadcast'), key: { id: 'broadcast', remoteJid: 'status@broadcast' } },
    message('system', { protocolMessage: {} }), message('valid')
  ] } })
  assert.deepEqual(h.rows().map(row => row.msg_id), ['valid'])
})

test('SIGTERM flushes metadata and removes only its own PID', async t => {
  const h = await harness(t)
  await h.emit({ 'contacts.upsert': [{ id: 'sender@lid', name: 'Example sender' }],
                 'groups.update': [{ id: 'example@g.us', subject: 'Example group' }] })
  assert.throws(() => h.process.emit('SIGTERM'), err => err instanceof Exit && err.status === 143)
  assert.equal(JSON.parse(fs.readFileSync(path.join(h.store, 'chats.json')))['example@g.us'].name, 'Example group')
  assert.equal(fs.existsSync(path.join(h.store, 'daemon.pid')), false)
})

test('unpaired daemon and drain exit 78 without contacting the transport', async t => {
  for (const command of ['daemon', 'drain']) {
    await assert.rejects(harness(t, { command, paired: false }), err => err instanceof Exit && err.status === 78)
  }
})

test('drain takes process ownership and waits for offline completion', async t => {
  const h = await harness(t, { command: 'drain', args: ['--seconds', '17'] })
  assert.equal(fs.readFileSync(path.join(h.store, 'daemon.pid'), 'utf8'), '12345')
  assert.deepEqual(h.timers.map(timer => timer.delay), [480000])
  await h.emit({ 'connection.update': { receivedPendingNotifications: true } })
  assert(h.timers.some(timer => timer.delay > 16000 && timer.delay <= 17000))
  assert.throws(() => h.timers[0].fn(), err => err instanceof Exit && err.status === 1)
  assert.equal(fs.existsSync(path.join(h.store, 'daemon.pid')), false)
})

test('storage is explicit and status does not create spool or credentials', async t => {
  await assert.rejects(harness(t, { missingStore: true }), err => err instanceof Exit && err.status === 78)
  const h = await harness(t, { command: 'status' })
  assert.equal(h.calls, 0)
  assert.deepEqual(fs.readdirSync(h.store), [])
})

test('recycled process IDs do not block a new daemon', async t => {
  const h = await harness(t, { before: store => fs.writeFileSync(path.join(store, 'daemon.pid'), '99999') })
  assert.equal(h.calls, 1)
  assert.equal(fs.readFileSync(path.join(h.store, 'daemon.pid'), 'utf8'), '12345')
})

test('drain rejects invalid timeout without opening a connection', async t => {
  for (const value of ['0', '480', 'bad']) {
    await assert.rejects(harness(t, { command: 'drain', args: ['--seconds', value] }), err => err instanceof Exit && err.status === 2)
  }
})
