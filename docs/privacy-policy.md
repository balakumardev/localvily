# Privacy Policy — Browser Relay (search & fetch)

_Last updated: 2026-06-20_

The **Browser Relay** Chrome extension turns your logged-in browser into a local
search-and-fetch backend for a self-hosted MCP server running on your own machine.
It is a developer tool. This policy explains exactly what it touches and where data
goes — which, by design, is nowhere except your own computer.

## What the extension does

The extension connects to a **local relay** that you run yourself, reachable only at
`http://localhost:15552` (configurable). It polls that relay for jobs. When the relay
hands it a job, the extension:

- opens the requested search-result page or URL in a tab,
- reads that page's content (search results, or the article's readable text), and
- posts the extracted result back to the **local** relay.

That's the entire data flow.

## What data is accessed

- **Page content** — only for pages the relay **explicitly drives**: the search-result
  pages it asks the extension to open, and the specific URLs it asks the extension to
  fetch. The extension does **not** read, monitor, log, or collect the content of any
  other tabs, your general browsing history, or pages you open yourself.
- **Your existing browser session** — the driven pages load using the cookies and login
  state already in your browser, so authenticated pages return their real content. The
  extension does not read, copy, or transmit your cookies or credentials; they never
  leave the browser.

## What data is stored

- The extension stores **only the relay server URL** (e.g. `http://localhost:15552`) in
  `chrome.storage.local`. Nothing else is persisted.
- No page content, search results, history, or personal data is stored by the extension.

## Where data is sent

- Extracted page content and search results are sent **only to the local relay** on your
  own machine (`localhost` / `127.0.0.1`).
- **No data is sent to the extension authors, to any third party, or to any remote
  server.** There is no telemetry, no analytics, no tracking, no advertising, and no
  external network calls beyond loading the pages you (via the relay) asked to fetch.

## Permissions and why they're needed

| Permission | Why |
|------------|-----|
| `tabs`, `scripting` | Open the requested page in a tab and extract its content |
| `storage` | Save the relay server URL you configure |
| `alarms` | Schedule the periodic poll of the local relay |
| `host_permissions` (`<all_urls>`, `localhost`) | Fetch arbitrary URLs you request, and talk to the local relay |

## Data sharing and sale

We do not collect, share, sell, or transfer any user data. There is no data to share —
everything stays on your machine.

## Changes

Any change to this policy will be reflected in this file in the project repository, with
an updated date above.

## Contact

This is an open-source developer tool. Questions and issues can be raised in the project's
source repository.
