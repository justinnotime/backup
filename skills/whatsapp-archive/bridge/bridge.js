#!/usr/bin/env node
// Receive-only linked-device spool. Device identity and history settings are
// preserved across reconnects; pairing, storage, and scheduling belong to callers.
import fs from 'node:fs'
import path from 'node:path'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'

const dependencyRoot = process.env.WHATSAPP_BRIDGE_DEPENDENCIES || fileURLToPath(new URL('.', import.meta.url))
const requireDependency = createRequire(path.join(dependencyRoot, 'package.json'))
const {
  default: makeWASocket, useMultiFileAuthState, makeCacheableSignalKeyStore,
  fetchLatestBaileysVersion, DisconnectReason, Browsers,
  extractMessageContent, getContentType, toNumber
} = await import(requireDependency.resolve('baileys'))
const { default: QRCode } = await import(requireDependency.resolve('qrcode'))
const { default: P } = await import(requireDependency.resolve('pino'))

// Bridge runtime. Tests execute this unchanged body with synthetic transports.
if (!process.env.WA_BRIDGE_DIR || !path.isAbsolute(process.env.WA_BRIDGE_DIR)) {
  console.error('WA_BRIDGE_DIR must explicitly name an absolute storage directory')
  process.exit(78)
}
const STORE = path.resolve(process.env.WA_BRIDGE_DIR)
const AUTH_DIR = path.join(STORE, 'auth')
const SPOOL_DIR = path.join(STORE, 'spool')
const CHATS_FILE = path.join(STORE, 'chats.json')
const CONTACTS_FILE = path.join(STORE, 'contacts.json')
const META_FILE = path.join(STORE, 'meta.json')
const PID_FILE = path.join(STORE, 'daemon.pid')

const logger = P({ level: process.env.WA_LOG_LEVEL || 'warn' }, P.destination(2))

// ---------------------------------------------------------------------------
// Small JSON stores
// ---------------------------------------------------------------------------
function loadJson (file, fallback) {
  try { return JSON.parse(fs.readFileSync(file, 'utf8')) } catch { return fallback }
}
function saveJson (file, obj) {
  fs.mkdirSync(path.dirname(file), { recursive: true })
  const tmp = file + '.tmp'
  fs.writeFileSync(tmp, JSON.stringify(obj, null, 1))
  fs.renameSync(tmp, file)
}

const chats = loadJson(CHATS_FILE, {})        // jid -> {name, type, last_ts}
const contacts = loadJson(CONTACTS_FILE, {})  // jid (pn or lid) -> name
const meta = loadJson(META_FILE, {})          // {lastGroupFetch}
let dirty = false
function flushNow () {
  if (!dirty) return
  dirty = false
  saveJson(CHATS_FILE, chats)
  saveJson(CONTACTS_FILE, contacts)
  saveJson(META_FILE, meta)
}
function persistSoon () {
  if (dirty) return
  dirty = true
  setTimeout(flushNow, 2000).unref()
}
// 'exit' runs on every process.exit() but NOT on default signal death — convert
// SIGINT/SIGTERM (Ctrl-C after login history sync, systemctl stop) into orderly
// exits so pending name-table writes and the pidfile unlink aren't lost.
process.on('exit', flushNow)
for (const sig of ['SIGINT', 'SIGTERM']) {
  process.on(sig, () => process.exit(sig === 'SIGINT' ? 130 : 143))
}

// ---------------------------------------------------------------------------
// Spool writer
// ---------------------------------------------------------------------------
function spool (row) {
  fs.mkdirSync(SPOOL_DIR, { recursive: true })
  const day = new Date().toISOString().slice(0, 10) // capture date, UTC
  fs.appendFileSync(path.join(SPOOL_DIR, `${day}.ndjson`), JSON.stringify(row) + '\n')
}

// ---------------------------------------------------------------------------
// Naming helpers
// ---------------------------------------------------------------------------
function isGroup (jid) { return typeof jid === 'string' && jid.endsWith('@g.us') }
function nameFor (jid, fallback) {
  return (chats[jid] && chats[jid].name) || contacts[jid] || fallback || ''
}
function rememberContact (jid, name) {
  if (jid && name && contacts[jid] !== name) { contacts[jid] = name; persistSoon() }
}
function rememberChat (jid, patch) {
  if (!jid || jid === 'status@broadcast') return
  const c = chats[jid] || (chats[jid] = { name: '', type: isGroup(jid) ? 'group' : 'dm', last_ts: 0 })
  if (patch.name) c.name = patch.name
  if (patch.last_ts && patch.last_ts > (c.last_ts || 0)) c.last_ts = patch.last_ts
  persistSoon()
}

// ---------------------------------------------------------------------------
// Message -> spool row
// ---------------------------------------------------------------------------
const MEDIA_KINDS = {
  imageMessage: 'image',
  videoMessage: 'video',
  audioMessage: 'audio',
  documentMessage: 'document',
  stickerMessage: 'sticker',
  ptvMessage: 'video-note'
}

function archive (msg, source) {
  const key = msg.key || {}
  const chatJid = key.remoteJid
  if (!chatJid || chatJid === 'status@broadcast') return
  const content = extractMessageContent(msg.message)
  if (!content) return // system stubs, reactions, protocol messages: skip
  const kind = getContentType(content)
  if (!kind) return

  const text = content.conversation ||
    (content.extendedTextMessage && content.extendedTextMessage.text) ||
    (content.imageMessage && content.imageMessage.caption) ||
    (content.videoMessage && content.videoMessage.caption) ||
    (content.documentMessage && content.documentMessage.caption) || ''

  let media = null
  const mediaKind = MEDIA_KINDS[kind]
  if (mediaKind) {
    const m = content[kind] || {}
    media = {
      kind: mediaKind,
      mime: m.mimetype || null,
      bytes: m.fileLength != null ? toNumber(m.fileLength) : null,
      filename: m.fileName || null,
      caption: m.caption || null,
      sha256: m.fileSha256 ? Buffer.from(m.fileSha256).toString('base64') : null
    }
  } else if (kind !== 'conversation' && kind !== 'extendedTextMessage') {
    if (!text) return // location/contact/poll/etc. with no text: skip for v1
  }
  if (!text && !media) return

  const ts = toNumber(msg.messageTimestamp) || Math.floor(Date.now() / 1000)
  const senderJid = key.fromMe ? 'me' : (key.participant || chatJid)
  const senderName = key.fromMe ? 'me' : (msg.pushName || nameFor(senderJid) || '')

  if (!key.fromMe && msg.pushName) rememberContact(senderJid, msg.pushName)
  if (!isGroup(chatJid) && !key.fromMe) {
    rememberChat(chatJid, { name: msg.pushName || nameFor(chatJid), last_ts: ts })
  } else {
    rememberChat(chatJid, { last_ts: ts })
  }

  spool({
    v: 1,
    ts,
    chat_jid: chatJid,
    chat_jid_alt: key.remoteJidAlt || null,
    chat_name: nameFor(chatJid, senderName),
    sender_jid: senderJid,
    sender_jid_alt: key.participantAlt || null,
    sender_name: senderName,
    msg_id: key.id,
    type: kind,
    text,
    media,
    from_me: !!key.fromMe,
    source
  })
}

// ---------------------------------------------------------------------------
// Socket lifecycle
// ---------------------------------------------------------------------------
let attempts = 0
let lastActivity = Date.now() // drain quiescence: bumped on every archived event

async function start (mode, opts) {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR)

  // QR pairing sets creds.me but NOT creds.registered (that flag belongs to
  // the pairing-code flow) — me.id is the reliable paired marker.
  const paired = !!(state.creds.registered || (state.creds.me && state.creds.me.id))
  if (mode !== 'login' && !paired) {
    console.error('not paired yet — run: node bridge.js login')
    process.exit(78) // EX_CONFIG; systemd RestartPreventExitStatus=78
  }
  if (mode === 'login' && paired) {
    console.log('already paired. Run the daemon (or drain).')
    process.exit(0)
  }

  const { version } = await fetchLatestBaileysVersion()
  const sock = makeWASocket({
    version,
    logger,
    auth: { creds: state.creds, keys: makeCacheableSignalKeyStore(state.keys, logger) },
    // Preserve the paired desktop identity and full-history request. The pinned
    // dependency's Windows platform mapping is adjusted by the package patch.
    browser: Browsers.windows('Desktop'),
    syncFullHistory: true,
    shouldSyncHistoryMessage: () => true,
    markOnlineOnConnect: false // keep push notifications on the phone
  })

  let drainTimer = null
  // one code per socket: baileys re-emits the qr event every 60s/20s, and each
  // requestPairingCode call would mint a NEW code, invalidating the one the
  // user is currently typing. A fresh socket (restart/reconnect) may request again.
  let pairingCodeRequested = false

  // Drain must not exit while messages/history chunks are still streaming in:
  // Baileys acks stanzas BEFORE our handler spools them, so anything in flight
  // at exit is gone for good. Exit only after graceMs of quiescence.
  const graceMs = (opts.seconds || 45) * 1000
  function armDrainExit () {
    const idle = Date.now() - lastActivity
    if (idle < graceMs) {
      drainTimer = setTimeout(armDrainExit, graceMs - idle)
      return
    }
    console.log('drain quiescent; exiting')
    sock.end(undefined)
    process.exit(0)
  }

  sock.ev.process(async (events) => {
    if (events['creds.update']) await saveCreds()

    if (events['connection.update']) {
      const { connection, lastDisconnect, qr, receivedPendingNotifications } = events['connection.update']

      if (qr) {
        if (mode === 'login') {
          if (opts.pairingCode) {
            if (!pairingCodeRequested && !sock.authState.creds.registered) {
              pairingCodeRequested = true
              const code = await sock.requestPairingCode(opts.pairingCode)
              console.log(`\npairing code (enter on phone under Linked Devices > Link with phone number):\n\n    ${code}\n`)
            }
            // later qr rotations: keep the already-printed code valid
          } else {
            console.log(await QRCode.toString(qr, { type: 'terminal', small: true }))
            console.log('scan with WhatsApp on the phone: Settings > Linked Devices > Link a Device')
          }
        } else {
          console.error('session lost pairing — run: node bridge.js login')
          process.exit(78)
        }
      }

      if (connection === 'open') {
        attempts = 0
        console.log(`[${new Date().toISOString()}] connected (${mode})`)
        refreshGroups(sock).catch(err => logger.warn({ err }, 'group refresh failed'))
      }

      if (receivedPendingNotifications) {
        console.log(`[${new Date().toISOString()}] offline queue drained`)
        if (mode === 'drain' && !drainTimer) {
          lastActivity = Date.now()
          armDrainExit()
        }
      }

      if (connection === 'close') {
        clearTimeout(drainTimer)
        drainTimer = null
        const code = lastDisconnect && lastDisconnect.error &&
          lastDisconnect.error.output && lastDisconnect.error.output.statusCode
        if (code === DisconnectReason.loggedOut) {
          console.error('device logged out; stop the bridge and pair again after preserving the existing store')
          process.exit(78)
        }
        if (code === DisconnectReason.connectionReplaced) {
          console.error('connection replaced — another bridge process is running with the same auth. Exiting.')
          process.exit(1)
        }
        if (code === DisconnectReason.restartRequired) {
          console.log('restart required (normal after pairing); reconnecting')
          return void start(mode === 'login' ? 'daemon-after-login' : mode, opts)
        }
        if (mode === 'drain') {
          console.error(`connection closed (code ${code}) during drain; exiting`)
          process.exit(1)
        }
        const delay = Math.min(1000 * 2 ** attempts++, 60000)
        console.error(`connection closed (code ${code}); reconnecting in ${delay / 1000}s`)
        setTimeout(() => start(mode, opts), delay)
      }
    }

    // Initial pairing backfill + on-demand history: archive as source 'history'.
    if (events['messaging-history.set']) {
      lastActivity = Date.now()
      const { messages = [], chats: histChats = [], contacts: histContacts = [], progress, isLatest } = events['messaging-history.set']
      for (const c of histContacts) {
        rememberContact(c.id, c.name || c.notify)
        if (c.phoneNumber) rememberContact(c.phoneNumber, c.name || c.notify)
      }
      for (const c of histChats) rememberChat(c.id, { name: c.name || nameFor(c.id) })
      for (const m of messages) archive(m, 'history')
      console.log(`[${new Date().toISOString()}] history chunk: ${messages.length} msgs (progress=${progress ?? '?'} latest=${!!isLatest})`)
    }

    // Live ('notify') AND offline-queued ('append') — must persist both.
    if (events['messages.upsert']) {
      lastActivity = Date.now()
      const { messages = [] } = events['messages.upsert']
      for (const m of messages) archive(m, 'live')
    }

    if (events['contacts.upsert']) {
      for (const c of events['contacts.upsert']) rememberContact(c.id, c.name || c.notify)
    }
    if (events['contacts.update']) {
      for (const c of events['contacts.update']) if (c.id && (c.name || c.notify)) rememberContact(c.id, c.name || c.notify)
    }
    if (events['groups.update']) {
      for (const g of events['groups.update']) if (g.id && g.subject) rememberChat(g.id, { name: g.subject })
    }
  })

  return sock
}

async function refreshGroups (sock) {
  // group subjects power the whitelist matching; refresh at most every 6h
  const now = Date.now()
  if (meta.lastGroupFetch && now - meta.lastGroupFetch < 6 * 3600 * 1000) return
  const groups = await sock.groupFetchAllParticipating()
  for (const [jid, g] of Object.entries(groups)) rememberChat(jid, { name: g.subject })
  meta.lastGroupFetch = now
  persistSoon()
  console.log(`[${new Date().toISOString()}] group metadata refreshed: ${Object.keys(groups).length} groups`)
}

// ---------------------------------------------------------------------------
// PID guard (daemon/login write it; drain refuses to run beside a live daemon)
// ---------------------------------------------------------------------------
function bridgePidAlive (pid) {
  try {
    process.kill(pid, 0)
    // pid recycling guard: only trust a pid that is actually a bridge process
    return fs.readFileSync(`/proc/${pid}/cmdline`, 'utf8').includes('bridge.js')
  } catch { return false }
}
function writePid () {
  fs.mkdirSync(STORE, { recursive: true })
  fs.writeFileSync(PID_FILE, String(process.pid), { flag: 'wx' })
  process.on('exit', () => {
    try { if (fs.readFileSync(PID_FILE, 'utf8') === String(process.pid)) fs.unlinkSync(PID_FILE) } catch {}
  })
}
function daemonRunning () {
  let pid = 0
  try { pid = parseInt(fs.readFileSync(PID_FILE, 'utf8'), 10) } catch { return false }
  if (Number.isInteger(pid) && pid > 0 && bridgePidAlive(pid)) return true
  try { fs.unlinkSync(PID_FILE) } catch {} // stale pidfile: self-heal
  return false
}

// ---------------------------------------------------------------------------
// CLI
// ---------------------------------------------------------------------------
const argv = process.argv.slice(2)
const cmd = argv[0]

function flag (name, fallback) {
  const i = argv.indexOf(name)
  return i !== -1 && argv[i + 1] ? argv[i + 1] : fallback
}

switch (cmd) {
  case 'login': {
    if (daemonRunning()) {
      console.error('daemon is running — stop it first: systemctl --user stop whatsapp-bridge')
      process.exit(1)
    }
    writePid()
    console.log('after pairing completes, LEAVE THIS RUNNING (or start the systemd unit) so the initial history sync can stream in — it can take a while for full history.')
    start('login', { pairingCode: flag('--pairing-code', null) })
    break
  }
  case 'daemon': {
    if (daemonRunning()) { console.error('daemon already running'); process.exit(1) }
    writePid()
    start('daemon', {})
    break
  }
  case 'drain': {
    if (daemonRunning()) { console.log('daemon is running; spool is already live — nothing to drain'); process.exit(0) }
    const seconds = Number(flag('--seconds', '45'))
    if (!Number.isInteger(seconds) || seconds < 1 || seconds >= 480) {
      console.error('drain seconds must be an integer from 1 to 479'); process.exit(2)
    }
    writePid()
    start('drain', { seconds })
    // Exit orderly if the completion signal never arrives. The archive caller
    // allows another minute before terminating a stuck dependency.
    setTimeout(() => { console.error('drain hard timeout; exiting'); process.exit(1) }, 480 * 1000).unref()
    break
  }
  case 'chats': {
    const rows = Object.entries(chats).sort((a, b) => (b[1].last_ts || 0) - (a[1].last_ts || 0))
    if (!rows.length) { console.log('no chats known yet — pair and run the daemon first') }
    for (const [jid, c] of rows) {
      const last = c.last_ts ? new Date(c.last_ts * 1000).toISOString() : 'never'
      console.log(`[${(c.type || '?').padEnd(5)}] ${c.name || '(unnamed)'}  last=${last}\n        jid: ${jid}`)
    }
    break
  }
  case 'status': {
    const creds = loadJson(path.join(AUTH_DIR, 'creds.json'), {})
    const paired = !!(creds.registered || creds.me?.id)
    const days = fs.existsSync(SPOOL_DIR) ? fs.readdirSync(SPOOL_DIR).filter(f => f.endsWith('.ndjson')) : []
    console.log(`store:   ${STORE}`)
    console.log(`paired:  ${paired}`)
    console.log(`daemon:  ${daemonRunning() ? 'running' : 'not running'}`)
    console.log(`spool:   ${days.length} day file(s)${days.length ? `, latest ${days.sort().at(-1)}` : ''}`)
    console.log(`chats:   ${Object.keys(chats).length} known`)
    break
  }
  default:
    console.log('usage: node bridge.js <login [--pairing-code <E164-digits>] | daemon | drain [--seconds N] | chats | status>')
    process.exit(cmd ? 1 : 0)
}
