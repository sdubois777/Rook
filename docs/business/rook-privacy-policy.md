# Rook Privacy Policy

**Last updated: July 13, 2026**

This policy explains what data the Rook browser extension and the Rook web application collect, how we use it, where it is stored, and how you can delete it.

Rook is operated by **Rook Fantasy Football LLC**, a Colorado limited liability company.
1500 N Grant St, Ste N, Denver, CO 80203, USA
Contact: **rookadmin@rookff.com**

---

## Summary

- The Rook extension connects your Yahoo, ESPN, and Sleeper fantasy football accounts to your Rook account, keeps your leagues in sync, and relays your live draft so Rook can give real-time AI draft and roster advice.
- To sync ESPN leagues, the extension reads your ESPN session cookies (`espn_s2` and `SWID`) and sends them to the Rook backend. This happens **automatically** when you visit an ESPN fantasy page while signed in to Rook.
- During a live draft, the extension reads draft events (picks, bids, nominations) from the draft room page and sends them to Rook.
- **We do not sell or share your data with any third party.** There is no analytics, error tracking, advertising, or telemetry of any kind in the extension.
- You can delete your stored platform credentials at any time from **Account → Connected platforms → Disconnect**, or delete your entire account.

---

## 1. What the extension collects

### 1.1 Authentication information

**ESPN session cookies (`espn_s2` and `SWID`).**
When you are signed in to Rook and visit an ESPN fantasy page containing a league ID, the extension reads exactly two cookies from `fantasy.espn.com` — `espn_s2` and `SWID` — and sends them to the Rook backend.

This happens **automatically on page visit**. It is not triggered by a button click. We do this so that connecting an ESPN league requires no manual copying of session tokens, which is the only mechanism ESPN provides for third-party league access.

These cookies allow the Rook backend to read your ESPN league data (rosters, settings, standings) through ESPN's own interfaces. No other ESPN cookie is accessed. Cookie values are never written to our application logs.

**Your Rook API token.**
You paste your Rook draft token into the extension once, from your Rook Account page. It is stored locally in the browser via `chrome.storage.local` and sent to the Rook backend to identify your account on each request. It is not shared with Yahoo, ESPN, Sleeper, or anyone else.

### 1.2 Live draft content

During an active draft, the extension reads draft events from the draft room and sends them to the Rook backend. Depending on the platform this is done by reading the page contents (Yahoo, ESPN) or by observing the draft room's own real-time connection (Sleeper).

The data collected consists of:

- Player names and player IDs
- Bids, prices, and remaining auction budgets
- Team names, team IDs, and manager names (including other managers in your league)
- Roster contents, pick number, round, and whose turn it is
- Your platform user ID (used to identify which team in the draft is yours)

**Note on auction values:** "budget," "bid," and "price" refer to **in-game fantasy auction dollars**, not real currency. Rook does not process, collect, or have access to any real financial information.

This draft data is stored by Rook so we can show you your draft history and generate recommendations.

### 1.3 League sync signal

When you visit a Yahoo or ESPN fantasy page while signed in to Rook, the extension sends a signal to the Rook backend telling it to refresh your leagues for that platform. This request contains **only your Rook token** — no page contents, no browsing data, and no scraped information.

The Rook backend then fetches your league data itself, directly from the platform (using your stored ESPN cookies, or your Yahoo authorization). **The extension does not read or transmit your league data.**

### 1.4 What we do NOT collect

The extension does **not** collect:

- Browsing history or web navigation data
- Any data from websites other than the specific Yahoo, ESPN, and Sleeper fantasy pages listed in the extension's manifest
- Location data
- Health data
- Real financial or payment information
- Your real name or email address (these exist in your Rook account, but the extension does not access them)
- Clicks, scrolls, page views, or any behavioral analytics

---

## 2. Where your data goes

The extension communicates with exactly two hosts:

- **`fantasy.espn.com`** — solely as the source of the two ESPN cookies described above.
- **The Rook backend** — our own server, over HTTPS.

All transmission is encrypted in transit via HTTPS.

**We do not share, sell, rent, or transfer your data to any third party.** The extension contains no analytics services, no error-reporting services, no advertising networks, and no trackers.

---

## 3. Data storage and retention

**ESPN session cookies** are stored on our servers, **encrypted at rest**, and are retained until you delete them or they are overwritten by reconnecting. They are not stored in plaintext and are never logged.

**Draft events** are stored with your account so you can review your draft and so Rook can generate recommendations.

**Your Rook token** is stored locally in your browser by the extension and is removed when you uninstall the extension or clear its storage.

---

## 4. How to delete your data

**Disconnect a platform.** Go to **Account → Connected platforms** in the Rook web app and click **Disconnect** next to ESPN or Yahoo. This permanently deletes the stored credentials for that platform. Leagues you have already synced will remain visible but will no longer update until you reconnect.

**Delete your account.** Deleting your Rook account permanently deletes your stored credentials, leagues, and draft history.

**Remove the extension.** Uninstalling the extension removes the locally stored Rook token from your browser. Note that this does not by itself delete data already stored on Rook's servers — use Disconnect or account deletion for that.

For any other data request, contact **rookadmin@rookff.com**.

---

## 5. Permissions used by the extension

| Permission | Why it is needed |
|---|---|
| `cookies` | To read your ESPN session cookies (`espn_s2`, `SWID`) so Rook can sync your ESPN leagues on your behalf. This is the only method ESPN provides for third-party league access. Scoped to `fantasy.espn.com` only. |
| `storage` | To store your Rook API token locally in the browser, so you only have to enter it once. |
| Host access to Yahoo, ESPN, and Sleeper fantasy pages | To detect your league, and to read live draft events from the draft room during an active draft. The extension does not run on any other website. |

The extension requests no other permissions.

---

## 6. Children's privacy

Rook is not directed to children under 13 and we do not knowingly collect data from them.

---

## 7. Changes to this policy

We may update this policy. Material changes will be reflected in the "Last updated" date above and, where appropriate, communicated in the app.

---

## 8. Contact

Questions about this policy or your data: **rookadmin@rookff.com**
