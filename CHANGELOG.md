# Changelog

All notable changes to Stock Tracker. Each release's section is used as its GitHub release notes,
which the in-app updater shows before installing. Newest first.

## [1.1.0] - 2026-09-25

### Added
- **Stock history.** Every change to stock is now recorded in an append-only log: phone scans, scans held for a decision, manual edits, removals, catalog imports, imports undone, unnamed stock merged into named products, app resets, and settings changes. Entries can't be edited or deleted, and each is written in the same step as the change itself. A **🕘 History** window on the dashboard lets you filter by date, event type and search text, and **Export CSV** saves every matching event for analysis (stable event codes, plain numbers, ISO timestamps with the UTC offset, product name captured as it was at the time).
- **Check for updates.** **⚙ Settings → Check for updates** looks for a newer release on GitHub, shows what's new, and installs it in one click: the app backs up your data, downloads the installer, verifies its signature, closes, updates, and reopens by itself. Nothing is installed unless the download is signed with the publisher's key.
- **Database version check.** The app now refuses to open a database written by a newer version, with a clear message, instead of misreading it.
- **Expiry list export** to PDF or CSV, filtered by status and month, grouped, and sorted by any column.
- **Product catalog** with import, matching, editing and undo; **light and dark themes**; **Connect Mobile** shows an "open the app" QR code alongside the pairing QR.

### Changed
- **Reset App State** now keeps the stock history and logs each stock row it clears.
- The expiring-soon window is a setting (30, 60 or 90 days) in the setup wizard and in Settings.
- The Firebase service account key is stored encrypted and can only be replaced through the app.

### Upgrading from 1.0.0
Install the 1.1.0 setup file over the old version. Your data is kept. Stock that already exists appears in the history as "Starting stock", so the history adds up from the start. Later versions can be installed from inside the app.

## [1.0.0] - 2026-09-25

### Added
- First release: phone barcode scanner page, Firebase Firestore sync, and the Windows desktop app with local inventory, expiry highlighting, and a first-run setup wizard.
