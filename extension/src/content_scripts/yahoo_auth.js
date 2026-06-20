import { triggerPassiveSync } from '../utils/passive_sync.js'

// Signal extension presence to React app
window.__rook__ = true

// Passive sync on Yahoo Fantasy visit
triggerPassiveSync('yahoo')
