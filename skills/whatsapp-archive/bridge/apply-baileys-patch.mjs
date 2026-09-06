// Preserve the Windows desktop identity used by this pinned dependency.
// Fail if its platform map changes so dependency upgrades cannot silently alter it.
import fs from 'fs'

const FILE = new URL('./node_modules/baileys/lib/Utils/validate-connection.js', import.meta.url)
const OLD = 'Windows: proto.ClientPayload.WebInfo.WebSubPlatform.WIN32'
const NEW = 'Windows: proto.ClientPayload.WebInfo.WebSubPlatform.WIN_HYBRID'

let src
try {
  src = fs.readFileSync(FILE, 'utf8')
} catch {
  console.error('baileys patch: dependency file is missing')
  process.exit(1)
}
if (src.includes(NEW)) {
  console.log('baileys patch: already applied')
  process.exit(0)
}
if (!src.includes(OLD)) {
  console.error('baileys patch: PLATFORM_MAP pattern not found — dependency platform mapping changed; inspect before pairing')
  process.exit(1)
}
fs.writeFileSync(FILE, src.replace(OLD, NEW))
console.log('baileys patch: Windows Desktop -> WIN_HYBRID applied')
