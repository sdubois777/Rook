import { triggerPassiveSync } from '../utils/passive_sync.js'

// Signal extension presence to React app
window.__draftmind__ = true

// Passive sync on Yahoo Fantasy visit
triggerPassiveSync('yahoo')
