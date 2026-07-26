# APS Usage — Home Assistant Integration

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A custom Home Assistant integration for **Arizona Public Service (APS)** customers that provides energy usage, solar generation, and billing sensors — no APS mobile app or CSV download required.

> This project began as a fork of [Conexo-Casa/aps-csv-ha](https://github.com/Conexo-Casa/aps-csv-ha) and now develops independently, adding robust service-agreement detection and solar import/export support.

---

## Sensors

### All accounts

| Sensor | Entity ID | Unit | Description |
|--------|-----------|------|-------------|
| Yesterday kWh | `sensor.aps_yesterday_kwh` | kWh | Prior day's total electricity usage |
| Yesterday On-Peak kWh | `sensor.aps_yesterday_on_peak_kwh` | kWh | Prior day's on-peak (4–7 PM weekdays) usage |
| Yesterday Off-Peak kWh | `sensor.aps_yesterday_off_peak_kwh` | kWh | Prior day's off-peak usage |
| Current Billing Cycle kWh | `sensor.aps_current_billing_cycle_kwh` | kWh | Total kWh since current billing cycle started |
| 30-Day kWh | `sensor.aps_30_day_kwh` | kWh | Rolling 30-day energy total |
| Current Balance | `sensor.aps_current_balance` | USD | Outstanding bill amount |
| Bill Due Date | `sensor.aps_bill_due_date` | date | Next payment due date |
| Last Payment | `sensor.aps_last_payment` | USD | Most recent payment amount |

### Solar accounts (created automatically when APS flags your service agreement as solar)

| Sensor | Entity ID | Unit | Description |
|--------|-----------|------|-------------|
| Yesterday Grid Import kWh | `sensor.aps_yesterday_grid_import_kwh` | kWh | Energy drawn from the grid |
| Yesterday Grid Export kWh | `sensor.aps_yesterday_grid_export_kwh` | kWh | Solar energy sold back to the grid |
| Yesterday Solar Generated kWh | `sensor.aps_yesterday_solar_generated_kwh` | kWh | Total solar production |
| Yesterday Solar Self-Consumed kWh | `sensor.aps_yesterday_solar_self_consumed_kwh` | kWh | Solar energy used on-site |

> **⚠️ Solar support is experimental.** It was implemented from the APS dashboard's JavaScript (`getsummarizedusagesolardata` endpoint) but has not yet been verified against a live solar account. If you have solar and these sensors are unavailable or wrong, please [open an issue](https://github.com/damianharouff/aps-csv-ha/issues) with your debug logs — account numbers redacted.

All sensors update **every hour**. Energy sensors (kWh) are compatible with the [HA Energy Dashboard](https://www.home-assistant.io/docs/energy/).

---

## Requirements

- Home Assistant **2023.1** or newer
- An active **APS.com account** (aps.com login credentials)
- The `cryptography` Python package (automatically installed by HA)

---

## Installation

### Option A — HACS (Recommended)

1. Open HACS in Home Assistant.
2. Go to **Integrations** → click the **⋮ menu** (top right) → **Custom repositories**.
3. Enter the repository URL:
   ```
   https://github.com/damianharouff/aps-csv-ha
   ```
   Select **Integration** as the category and click **Add**.
4. Search for **APS Usage** in the HACS integrations list and click **Download**.
5. **Restart Home Assistant.**
6. Follow [Configuration](#configuration) below.

### Option B — Manual Installation

1. Clone this repository or download it as a ZIP.
2. Copy the `custom_components/aps_usage/` folder into your Home Assistant config directory:
   ```
   config/
   └── custom_components/
       └── aps_usage/
   ```
3. **Restart Home Assistant.**
4. Follow [Configuration](#configuration) below.

---

## Configuration

1. Go to **Settings → Devices & Services → Add Integration**.
2. Search for **APS Usage** and click it.
3. Enter your **APS.com username** and **password** (the same credentials you use at [aps.com](https://www.aps.com) or the APS mobile app).
4. Click **Submit**.

The integration authenticates, auto-detects your active service agreement (and production meter, for solar accounts), and creates all sensors. No account ID or meter number is needed — these are discovered automatically.

> **Multiple service addresses / agreements:** If your account has more than one service agreement (multiple addresses, solar production meters, closed accounts), the integration selects the **active consumption agreement** automatically. The `premise_address` attribute on each sensor shows which address is in use.

---

## Energy Dashboard

Go to **Settings → Dashboards → Energy**:

- **Grid consumption:** `sensor.aps_yesterday_kwh` (or `sensor.aps_yesterday_grid_import_kwh` on solar accounts)
- **Return to grid** (solar): `sensor.aps_yesterday_grid_export_kwh`
- **Solar production** (solar): `sensor.aps_yesterday_solar_generated_kwh`

Note that APS reports data with a ~1-day delay, so Energy Dashboard totals will trail real time.

---

## Sensor Attributes

All energy sensors carry `account_id`, `premise_address`, `latest_data_date`, and `bill_cycle_start`. `sensor.aps_current_billing_cycle_kwh` adds `rate_plan` (e.g. `R3-47`). The balance sensor carries `due_date`, `new_charges`, `auto_pay`, `budget_billing`, and last-payment details.

---

## Lovelace Example

```yaml
type: entities
title: APS Energy
entities:
  - entity: sensor.aps_yesterday_kwh
    name: Yesterday
  - entity: sensor.aps_current_billing_cycle_kwh
    name: This Billing Cycle
  - entity: sensor.aps_30_day_kwh
    name: Last 30 Days
  - entity: sensor.aps_yesterday_on_peak_kwh
    name: On-Peak (Yesterday)
  - entity: sensor.aps_yesterday_off_peak_kwh
    name: Off-Peak (Yesterday)
  - entity: sensor.aps_current_balance
    name: Current Balance
  - entity: sensor.aps_bill_due_date
    name: Due Date
```

---

## Update Frequency

The integration polls APS **once per hour**. APS updates usage data daily (not real-time), so energy readings reflect usage through the **previous day**. Balance and billing data reflect your APS.com balance at the last poll.

---

## Troubleshooting

### "Invalid Auth" error during setup
- Verify your credentials work at [aps.com](https://www.aps.com/en/Authorization/Login).
- APS rate-limits login attempts. Wait 5–10 minutes and try again.
- Use your **APS.com username** (not your email address or account number).

### Integration in "Setup Retry" / sensors unavailable
Check **Settings → System → Logs** and filter for `aps_usage`. Common causes:

| Error | Cause | Fix |
|-------|-------|-----|
| `Authentication failed` | Wrong password / rate limited | Re-check credentials; wait 10 min |
| `Connection error` | HA can't reach aps.com | Check HA network / DNS |
| `No active service agreement` | Unrecognized account structure | See below |
| `Login request blocked` | Imperva WAF challenge | Retry in 15 minutes (auto-recovers) |

### "No active service agreement found"
The integration handles many account layouts (multiple agreements, solar production meters, placeholder end dates, single-item responses). If yours still isn't recognized, the log will contain a warning starting with `APS: No service agreement (SASP) found` that includes the **key structure** of the API response (values stripped — no personal data). Please [open an issue](https://github.com/damianharouff/aps-csv-ha/issues) and paste that warning.

### Solar sensors unavailable
The solar endpoint is best-effort: if it fails, the core usage sensors keep working and the solar sensors go unavailable. Enable debug logging (below) and look for `APS: Solar usage API` or `no production meter` warnings, then open an issue with what you find.

### Enable debug logging
Add to `configuration.yaml` and restart:
```yaml
logger:
  default: warning
  logs:
    custom_components.aps_usage: debug
```

---

## How It Works

This integration was reverse-engineered from the APS website (`aps.com`) and the Android mobile app (`com.aps.apsconsumerapp` v4.0.10, decompiled with jadx + hermes-dec).

### Authentication Flow
1. **Password encryption:** The password is encrypted with an RSA-2048 public key using PKCS#1 v1.5 (replicating the `JSEncrypt` library used by the APS website login form, key extracted from `aps-apscom.js`).
2. **Login:** `POST https://www.aps.com/api/sitecore/SitecoreReactApi/UserAuthentication` — returns `{isLoginSuccess: true, redirectUrl: "..."}`.
3. **Session establishment:** `GET {redirectUrl}` — establishes ASP.NET session cookies.
4. **Token retrieval:** `GET https://www.aps.com/api/sitecore/sitecorereactapi/GetAllUserDetails` — returns the Azure AD B2C access token (`B2C_AccessToken`), account details, and `getSASPListByAccountID` containing all service agreements with meter numbers.

### Service Agreement Selection
Every service agreement / service point (SASP) in the account is collected — tolerating single-item dict responses, key-casing differences, and far-future placeholder end dates. The **active consumption agreement** is chosen (non-solar type, has a service point). On solar accounts, the **production meter** is identified as the active SASP with a distinct service point.

### Usage Data
```
GET https://mobi.aps.com/ccb-billing/v1/getdailyusagecharges
```
Query parameters: `action=read`, `accountNumber`, `userName`, `emailAddress`, `sAID` (Service Agreement ID), `spId` (Service Point ID), `startDate`, `endDate`, `cSSUser=APSCOM`. The response contains a `series` array (one entry per day) with `totalUsage`, `onPeakUsage`, `offPeakUsage`, temperature, and charge amounts, plus `billCycleDates` marking billing cycle boundaries.

### Solar Data (solar accounts)
```
POST https://mobi.aps.com/customerhistoryservices/v1/getsummarizedusagesolardata
```
JSON body includes both meters: `utilityMeterNumber`/`utilityMeterSPId` and `prodMeterNumber`/`prodMeterSPId`, plus `saId`, `premiseId`, `displayType: "D"`, a date range, and a `ratePlan` array. The response's `Series` array carries per-day `totalAPSEnergyUsed` (grid import), `totalPowerGenerated` (production), `totalGenerationSold` (export), and `totalGenerationUsed` (self-consumed), each with actual + estimated variants.

### Financial Data
Extracted directly from the `GetAllUserDetails` response — `getAccountFinancialDetails` contains `currentBalance`, `dueDt`, `lastPayAmt`, `lastPayDt`, and `newCharges`. No additional API call is needed.

---

## Privacy & Security

- Credentials are stored in **Home Assistant's encrypted config entry storage**. They are never logged or transmitted to any third party.
- All network requests go directly to `www.aps.com` and `mobi.aps.com`.
- The integration is **read-only** — it makes no changes to your APS account.
- Diagnostic log output strips values from API responses (key names only).

---

## Contributing

Pull requests are welcome! When reporting a bug, please include:
1. Your Home Assistant version (`Settings → About`)
2. The integration version
3. Relevant log entries (with **all credentials and account numbers redacted**)

---

## License

[MIT License](LICENSE)

---

## Credits & Disclaimer

Originally based on [Conexo-Casa/aps-csv-ha](https://github.com/Conexo-Casa/aps-csv-ha).

This integration is not affiliated with, endorsed by, or supported by Arizona Public Service Company (APS). It uses undocumented private APIs that APS may change at any time. Use at your own risk.
