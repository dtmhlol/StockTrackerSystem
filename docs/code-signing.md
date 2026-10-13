# Code signing and Smart App Control

Researched October 2026. No signing is wired into the build yet (decided for 1.1.3: research only).

## The problem
Smart App Control (Windows 11) blocks apps that are not signed by a certificate from a CA Microsoft
trusts, or that have no good reputation. A **self-signed certificate does not help**. The release
signature already used by the in-app updater (Ed25519, `installer/release_tools.py`) is a separate
thing and does not count for Windows.

## Options
| Option | Cost | Catch |
|---|---|---|
| [Azure Artifact Signing](https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/code-signing-options) (formerly Trusted Signing) | $9.99/month (Basic) | Microsoft's recommended route and trusted by Smart App Control, no hardware token, works from CI. Needs an Azure subscription and identity validation. Individuals: USA and Canada only. Organizations need a verifiable operating history. |
| [SignPath Foundation](https://about.signpath.io/product/open-source) | Free | Needs an OSI-approved open-source license (this repo has no LICENSE file yet), an application, and a fully automated build that signs from the repository (GitHub Actions). Policy text must appear on the project page. |
| OV / EV certificate from a CA | Several hundred USD per year | Private keys now live on hardware tokens or cloud HSMs; OV may still need reputation to build up. |

## What would change in this repo
1. `installer/release.ps1`: after building, sign `dist\StockTracker\StockTracker.exe` (before the installer
   is built, so the installed exe is signed) and then the installer, using `signtool` with the chosen
   service's dlib/plugin, then verify with `signtool verify /pa`.
2. `installer/StockTracker.iss`: add `SignTool=` and `SignedUninstaller=yes`.
3. Keep `release_tools.py sign` as is; it signs the already Authenticode-signed installer.

## Recommendation
If you can use Azure Artifact Signing (US/Canada), choose it. Otherwise make the repo open source with a
LICENSE and apply to SignPath Foundation.
