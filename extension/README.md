# DraftMind Browser Extension

Chrome MV3 / Firefox 109+ extension for DraftMind fantasy football platform.

## Features

- **ESPN cookie extraction** — Automatically captures `espn_s2` and `SWID` cookies when you visit ESPN Fantasy, replacing the manual bookmarklet
- **Live draft relay** — Intercepts WebSocket frames from Yahoo/ESPN/Sleeper draft rooms and forwards them to the DraftMind backend
- **Passive sync** — Triggers league data sync when you visit any connected fantasy platform (30-minute debounce)
- **Capture mode** — Debug toggle that stores raw WS frames locally for parser development

## Setup

1. Get your draft token from **Account > Browser Extension** in the DraftMind app
2. Install the extension (see below)
3. Click the extension icon and paste your draft token
4. Visit your fantasy platform — the extension syncs automatically

## Development

```bash
cd extension
npm install
npm run dev    # webpack --watch
npm run build  # production build → dist/
```

Load `dist/` as an unpacked extension in `chrome://extensions` (enable Developer mode).

## Architecture

```
src/
├── background/
│   └── service_worker.js    # Message relay (content scripts → backend API)
├── content_scripts/
│   ├── yahoo_draft.js       # Yahoo draft room WS interceptor
│   ├── espn_draft.js        # ESPN draft room WS interceptor
│   ├── sleeper_draft.js     # Sleeper draft room WS interceptor
│   ├── espn_auth.js         # ESPN cookie extraction + passive sync
│   └── yahoo_auth.js        # Yahoo passive sync
├── popup/
│   ├── popup.html
│   ├── popup.css
│   └── popup.js             # Token entry, platform status, capture toggle
└── utils/
    ├── browser.js            # Chrome/Firefox API shim
    ├── api.js                # Backend API helpers (draft token, events)
    ├── passive_sync.js       # 30-min debounced platform sync
    └── ws_interceptor.js     # WebSocket monkey-patch (MAIN world injection)
```

## Draft Room Parsers

The `parseYahooFrame()`, `parseESPNFrame()`, and `parseSleeperFrame()` functions are stubs that return `null`. They will be implemented once real WebSocket frame samples are captured using capture mode.

To capture frames:
1. Enable **Capture mode** in the extension popup
2. Open a live draft room
3. After the draft, click **Export captured frames** to download the JSON
4. Use the exported frames to build the parser logic

## Auth Flow

The extension uses a long-lived UUID **draft token** (not JWT) stored in `browser.storage.local`. The token is sent as `X-Draft-Token` header on all backend requests. Tokens can be revoked and regenerated from the Account page.
