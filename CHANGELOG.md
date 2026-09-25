# Changelog

All notable changes to Stock Tracker. Each release's section is used as its GitHub release notes,
which the in-app updater shows before installing. Newest first.

## [1.1.2] - 2026-09-26

### Added
- **Add Item.** A new **＋ Add Item** button (or Ctrl+N) records stock by hand, for example an item found on a shelf that is close to expiring. Type or scan the barcode or item code and the catalog fills in the product name. Choose the expiry month (2027-03 or 03/2027 both work; the window shows whether it counts as expired or expiring soon), and the quantity. It behaves like a phone scan: an existing row for the same product and expiry goes up. Codes the catalog doesn't know can be added with a name (a new product) or without one (an unnamed product). If a code matches several products you choose which. Tick "Keep this window open" to add several items in a row. Each one appears in History as **Manual add**.
- **Replace or reset the product catalog** (⚙ Settings → Product catalog). **Replace from file...** makes a CSV the whole catalog, with the usual column choice and a preview of exactly what will change. **Reset catalog...** clears every product. Stock on hand is always kept: it stays under its barcode as an unnamed product and is matched to the new catalog by barcode the next time products are imported or replaced. A safety backup is saved first (in the `backups` folder next to the database), both need a confirmation (Reset asks you to type RESET), both are recorded in History, and both can be undone from the Products window until stock next changes.

### Changed
- **Undo last import** is now **Undo last change**, because it also undoes a replace or reset.
- The dashboard's **Add Item**, **Remove Selected** and **Refresh** buttons now sit in their own row above the stock table.
- History has two new event types you can filter on: **Manual add** and **Catalog reset / replaced**.

### Fixed
- **Buttons cut off on small screens.** On a short screen, the buttons at the bottom of some windows (for example **Preview changes / Cancel / Import** when importing a product list) ran off the bottom edge and couldn't be clicked. Every window now fits the space above the taskbar, and its buttons are pinned to the bottom edge so they always show. If the content is taller than the screen, the content scrolls (scrollbar or mouse wheel) while the buttons stay in place. This applies to Import and Replace, Add Item, Edit, Export, Settings, Connect Mobile, Pending scans, the Products window, History, Update and Setup. Windows also open centred over the main window and never partly off the screen.
- On some screens the **Remove Selected** and **Refresh** buttons were pushed off the right edge of the toolbar and couldn't be seen. The window now never becomes narrower than its buttons need.

### Upgrading from 1.1.0
Use **Settings → Check for updates**. Your data is kept, and the database format is unchanged.

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
