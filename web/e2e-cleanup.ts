import { rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

export default function cleanup() {
  const directory = process.env.LABELCHECK_E2E_DATA_DIR
  if (directory?.startsWith(join(tmpdir(), 'labelcheck-e2e-'))) {
    rmSync(directory, { recursive: true, force: true })
  }
}
