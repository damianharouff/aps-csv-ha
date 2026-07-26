"""API Client for APS Usage — reverse-engineered from apsconsumerapp APK."""

from __future__ import annotations

import base64
import logging
import urllib.parse
from datetime import datetime, timedelta
from typing import Any

import aiohttp
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

_LOGGER = logging.getLogger(__name__)

# RSA-2048 public key from aps-apscom.js (used by JSEncrypt for login)
APS_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAgUhnZn9KwG21odw0+4Jf
Ie/pdOd+Ry8sdxn4tnmkfZJZ8/5xV31Zi6QqIxoiOQrdROyJaDBtbv0KGS68Yfim
gqOpD9873Yp+PhN+VhurJsVX8a2UibdvrPIDOhe5+9Z/BPd5TeEhMK59Hvm7Z+pn
lFObF9DMGxfbUDUCU37lHkkz3rJONaPMXdUSJFGL+6VwFNCkj7tmusgQsLLzCOsx
miMgGOI+Wk1Nx9vCDOu9f9TaznrqTc9sFk/2dOQULDg7VQoeFoF8PjrZG3eEVZG
XFRaJBG+4mX4Vercms2J8u1NIeFdFeTjuo+nAiDsc0z4J9g3gVPC+k2080EBkqHw
ycwIDAQAB
-----END PUBLIC KEY-----"""

# From BuildConfig.java in com.aps.apsconsumerapp APK
OCP_APIM_KEY = "d2e9aafca6d546cd9097a3e3072cd7a5"

# Must match a real Chrome UA to pass Imperva WAF on aps.com
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# APS Sitecore web API base
SITECORE_BASE = "https://www.aps.com"
LOGIN_URL = f"{SITECORE_BASE}/api/sitecore/SitecoreReactApi/UserAuthentication"
USER_DETAILS_URL = f"{SITECORE_BASE}/api/sitecore/sitecorereactapi/GetAllUserDetails"

# mobi.aps.com — daily usage charges (GET with query params, discovered from Dashboard.js)
DAILY_USAGE_URL = "https://mobi.aps.com/ccb-billing/v1/getdailyusagecharges"

# mobi.aps.com — solar generation/export data (POST, used by the dashboard
# when the selected service agreement has isSolar=true)
SOLAR_USAGE_URL = (
    "https://mobi.aps.com/customerhistoryservices/v1/getsummarizedusagesolardata"
)

# CSS_USER constant from Accounts/Dashboard.js bundle
CSS_USER = "APSCOM"


def _encrypt_password(password: str) -> str:
    """Encrypt password with APS RSA public key (PKCS#1 v1.5, same as JSEncrypt)."""
    public_key = serialization.load_pem_public_key(APS_PUBLIC_KEY.encode("utf-8"))
    assert isinstance(public_key, RSAPublicKey)
    encrypted = public_key.encrypt(password.encode("utf-8"), padding.PKCS1v15())
    return base64.b64encode(encrypted).decode("utf-8")


def _normalize_key(key: str) -> str:
    """Normalize a JSON key for tolerant matching (sAID == saId == sa_id)."""
    return "".join(ch for ch in key.lower() if ch.isalnum())


def _ci_get(data: Any, *names: str) -> Any:
    """Case/format-insensitive dict lookup."""
    if not isinstance(data, dict):
        return None
    wanted = {_normalize_key(n) for n in names}
    for key, value in data.items():
        if _normalize_key(str(key)) in wanted:
            return value
    return None


def _as_list(value: Any) -> list:
    """Coerce to list — CCB endpoints return a bare dict for 1-item collections."""
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    return []


def _sa_is_active(sa_end: str) -> bool:
    """Whether an SA end date means "still active".

    Active agreements show up with an empty end date on most accounts, but
    some (e.g. solar / TOU plans) carry a far-future placeholder instead.
    """
    text = str(sa_end or "").strip()
    if not text:
        return True
    if "9999" in text:
        return True
    for candidate in (text, text[:10]):
        for fmt in ("%Y-%m-%d", "%m-%d-%Y", "%m/%d/%Y", "%d-%m-%Y"):
            try:
                return datetime.strptime(candidate, fmt) > datetime.now()
            except ValueError:
                continue
    return False


def _describe_structure(node: Any, depth: int = 0) -> Any:
    """Key structure of a JSON payload without any values (safe to log)."""
    if depth > 8:
        return "..."
    if isinstance(node, dict):
        return {k: _describe_structure(v, depth + 1) for k, v in node.items()}
    if isinstance(node, list):
        return [_describe_structure(node[0], depth + 1)] if node else []
    return type(node).__name__


def _extract_sasp_candidates(details: dict) -> list[dict]:
    """Find every SASP (service agreement / service point) in GetAllUserDetails.

    Tries the documented path first, tolerating dict-vs-list and key-casing
    quirks; if that yields nothing, recursively scans the whole payload for
    any object carrying an sAID.
    """
    candidates: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(sasp: Any, premise: Any = None) -> None:
        if not isinstance(sasp, dict):
            return
        sa_id = _ci_get(sasp, "sAID")
        sp_type = str(_ci_get(sasp, "sPType") or "")
        # Production-meter entries (sPType=PROD) may carry no SA ID; keep
        # them anyway — they're needed to build the solar data request.
        if sa_id in (None, "") and sp_type.upper() != "PROD":
            return
        entry = {
            "sa_id": str(sa_id or ""),
            "sp_type": sp_type,
            "sp_id": str(_ci_get(sasp, "sPID") or ""),
            "premise_id": str(
                _ci_get(sasp, "premiseID") or _ci_get(premise, "premiseID") or ""
            ),
            "premise_address": str(
                _ci_get(sasp, "premiseAddress")
                or _ci_get(premise, "premiseAddress")
                or ""
            ),
            "sa_end": str(_ci_get(sasp, "sAEndDate") or ""),
            "sa_type": str(
                _ci_get(
                    sasp, "sAType", "sATypeCd", "sATypeCode", "sATypeDesc",
                    "sATypeDescription",
                )
                or ""
            ),
            "meter": str(_ci_get(sasp, "meterBadgeNumber", "meterNum") or ""),
            "is_solar": str(_ci_get(sasp, "isSolar") or "").lower()
            in ("true", "y", "yes", "1"),
            # APS spells this "sARatePlancCode" in Dashboard.js — match both
            "rate_plan_code": str(
                _ci_get(sasp, "sARatePlancCode", "sARatePlanCode") or ""
            ),
            "sa_start": str(
                _ci_get(sasp, "sAStartDate", "sARatePlanEffDate") or ""
            ),
        }
        key = (entry["sa_id"], entry["sp_id"])
        if key in seen:
            return
        seen.add(key)
        candidates.append(entry)

    acct_res = _ci_get(
        _ci_get(_ci_get(details, "AccountDetails"), "getAccountDetailsResponse"),
        "getAccountDetailsRes",
    )
    premise_list = _as_list(
        _ci_get(_ci_get(acct_res, "getSASPListByAccountID"), "premiseDetailsList")
    )
    for premise in premise_list:
        for sasp in _as_list(_ci_get(premise, "sASPDetails")):
            add(sasp, premise)

    if candidates:
        return candidates

    # Fallback: some account types nest SASP data differently — scan everything.
    def walk(node: Any, premise: Any = None) -> None:
        if isinstance(node, dict):
            if _ci_get(node, "sAID") not in (None, "") or str(
                _ci_get(node, "sPType") or ""
            ).upper() == "PROD":
                add(node, premise)
            nearest = node if _ci_get(node, "premiseID") not in (None, "") else premise
            for value in node.values():
                walk(value, nearest)
        elif isinstance(node, list):
            for item in node:
                walk(item, premise)

    walk(details)
    return candidates


def _choose_sasp(candidates: list[dict]) -> dict | None:
    """Pick the consumption SASP: has an SA, not a production meter, active.

    Solar accounts carry a production-meter entry (sPType=PROD, often with
    no SA ID) alongside the consumption one; usage data lives on the
    consumption agreement.
    """
    consumption = [
        c for c in candidates if c["sa_id"] and c["sp_type"].upper() != "PROD"
    ]
    if not consumption:
        return None
    pool = [c for c in consumption if _sa_is_active(c["sa_end"])] or consumption
    non_solar = [c for c in pool if "SOLAR" not in c["sa_type"].upper()] or pool
    with_sp = [c for c in non_solar if c["sp_id"]] or non_solar
    return with_sp[0]


def _find_production_meter(candidates: list[dict], chosen: dict) -> dict | None:
    """Locate the solar production meter's SASP entry.

    The dashboard identifies it by sPType == "PROD". Fall back to any other
    active SASP with a distinct service point, preferring solar-typed ones.
    """
    prod_typed = [
        c
        for c in candidates
        if c["sp_type"].upper() == "PROD" and (c["sp_id"] or c["meter"])
    ]
    if prod_typed:
        return prod_typed[0]
    others = [
        c
        for c in candidates
        if c["sp_id"]
        and c["sp_id"] != chosen["sp_id"]
        and _sa_is_active(c["sa_end"])
    ]
    if not others:
        return None
    for c in others:
        sa_type = c["sa_type"].upper()
        if any(hint in sa_type for hint in ("SOLAR", "GEN", "PROD")):
            return c
    return others[0]


def _payload_flags_solar(details: dict) -> bool:
    """Whether any premise carries a solar marker.

    The dashboard treats an account as solar via premise-level flags
    (isProductionMeter / isBidirectionalMeter / isSolar) rather than a
    field on the SASP entries themselves.
    """
    found = False

    def walk(node: Any) -> None:
        nonlocal found
        if found:
            return
        if isinstance(node, dict):
            for key in ("isProductionMeter", "isBidirectionalMeter", "isSolar"):
                if str(_ci_get(node, key) or "").lower() in (
                    "true",
                    "y",
                    "yes",
                    "1",
                ):
                    found = True
                    return
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(details)
    return found


class APSAuthError(Exception):
    """Raised when APS authentication fails."""


class APSUsageData:
    """Container for parsed daily usage data."""

    def __init__(
        self,
        series: list[dict],
        bill_cycle_dates: list[dict],
        account_id: str,
        sa_id: str,
        sp_id: str,
        premise_id: str,
        premise_address: str,
    ) -> None:
        self.series = series
        self.bill_cycle_dates = bill_cycle_dates
        self.account_id = account_id
        self.sa_id = sa_id
        self.sp_id = sp_id
        self.premise_id = premise_id
        self.premise_address = premise_address

    @property
    def yesterday_kwh(self) -> float | None:
        """Return yesterday's total kWh usage."""
        # series is ordered oldest→newest; skip last entry (today, often empty)
        for item in reversed(self.series[:-1]):
            val = item.get("totalUsage") or item.get("totalDailyUsage")
            if val:
                try:
                    return float(val)
                except (ValueError, TypeError):
                    pass
        return None

    @property
    def today_kwh(self) -> float | None:
        """Return today's kWh usage (may be partial/estimated)."""
        if not self.series:
            return None
        val = self.series[-1].get("totalUsage") or self.series[-1].get(
            "totalDailyUsage"
        )
        try:
            return float(val) if val else None
        except (ValueError, TypeError):
            return None

    @property
    def current_cycle_kwh(self) -> float:
        """Total kWh since the most recent billing cycle start date."""
        if not self.bill_cycle_dates:
            # Fall back: sum all available series
            return self.period_kwh(len(self.series))
        cycle_start_str = self.bill_cycle_dates[0].get("billCycleDate", "")
        try:
            cycle_start = datetime.strptime(cycle_start_str, "%Y-%m-%d")
        except ValueError:
            return self.period_kwh(len(self.series))
        total = 0.0
        for item in self.series:
            try:
                item_date = datetime.strptime(item["date"], "%Y-%m-%d")
                if item_date >= cycle_start:
                    val = item.get("totalUsage") or item.get("totalDailyUsage") or 0
                    total += float(val)
            except (ValueError, TypeError, KeyError):
                pass
        return round(total, 2)

    @property
    def current_bill_cycle_start(self) -> str | None:
        """Start date of the current billing cycle."""
        if self.bill_cycle_dates:
            return self.bill_cycle_dates[0].get("billCycleDate")
        return None

    def period_kwh(self, days: int) -> float:
        """Total kWh over the last N days."""
        recent = self.series[-days:] if len(self.series) >= days else self.series
        total = 0.0
        for item in recent:
            try:
                total += float(
                    item.get("totalUsage") or item.get("totalDailyUsage") or 0
                )
            except (ValueError, TypeError):
                pass
        return round(total, 2)

    @property
    def latest_date(self) -> str | None:
        """Date of the most recent data point with actual usage."""
        for item in reversed(self.series):
            val = item.get("totalUsage") or item.get("totalDailyUsage")
            if val:
                return item.get("date")
        return None

    @property
    def on_peak_kwh_yesterday(self) -> float | None:
        """Yesterday's on-peak kWh."""
        for item in reversed(self.series[:-1]):
            val = item.get("onPeakUsage")
            if val:
                try:
                    return float(val)
                except (ValueError, TypeError):
                    pass
        return None

    @property
    def off_peak_kwh_yesterday(self) -> float | None:
        """Yesterday's off-peak kWh."""
        for item in reversed(self.series[:-1]):
            val = item.get("offPeakUsage")
            if val:
                try:
                    return float(val)
                except (ValueError, TypeError):
                    pass
        return None


class APSSolarData:
    """Container for parsed solar generation data (per-day Series).

    Field names come from the Dashboard.js solar chart mapping. Actual and
    estimated readings arrive in separate fields; values here sum both.
    """

    _IMPORT = ("totalAPSEnergyUsed", "totalAPSEnergyUsedEstimated")
    _GENERATED = ("totalPowerGenerated", "totalPowerGeneratedEstimated")
    _EXPORTED = ("totalGenerationSold", "totalGenerationSoldEstimated")
    _SELF_USED = ("totalGenerationUsed", "totalGenerationUsedEstimated")

    def __init__(self, series: list[dict]) -> None:
        self.series = series

    @staticmethod
    def _value(item: dict, *keys: str) -> float | None:
        total = None
        for key in keys:
            raw = _ci_get(item, key)
            if raw in (None, ""):
                continue
            try:
                total = (total or 0.0) + float(raw)
            except (ValueError, TypeError):
                continue
        return total

    @property
    def _latest_item(self) -> dict | None:
        """Most recent day (excluding today when possible) with any real data."""
        candidates = self.series[:-1] or self.series
        for item in reversed(candidates):
            for keys in (
                self._IMPORT,
                self._GENERATED,
                self._EXPORTED,
                self._SELF_USED,
            ):
                if self._value(item, *keys):
                    return item
        return None

    def _latest(self, keys: tuple[str, ...]) -> float | None:
        item = self._latest_item
        if item is None:
            return None
        # 0.0 is meaningful here (e.g. nothing exported on a cloudy day)
        return round(self._value(item, *keys) or 0.0, 2)

    @property
    def grid_import_yesterday(self) -> float | None:
        """kWh drawn from the grid on the latest complete day."""
        return self._latest(self._IMPORT)

    @property
    def generated_yesterday(self) -> float | None:
        """Total solar kWh produced on the latest complete day."""
        return self._latest(self._GENERATED)

    @property
    def exported_yesterday(self) -> float | None:
        """Solar kWh sold back to the grid on the latest complete day."""
        return self._latest(self._EXPORTED)

    @property
    def self_used_yesterday(self) -> float | None:
        """Solar kWh consumed on-site on the latest complete day."""
        return self._latest(self._SELF_USED)

    @property
    def latest_date(self) -> str | None:
        """Date of the latest complete day of solar data."""
        item = self._latest_item
        if item is None:
            return None
        val = _ci_get(item, "date", "usageDate", "readDate")
        return str(val) if val else None


class APSUsageAPI:
    """APS Usage API — uses web B2C token with mobi.aps.com billing endpoints."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str,
        password: str,
    ) -> None:
        self._session = session
        self._username = username
        self._password = password
        self._b2c_access_token: str | None = None
        self._token_expiry: datetime | None = None
        self._account_id: str | None = None
        self._email: str | None = None
        # Active service agreement details (from getSASPListByAccountID)
        self._sa_id: str | None = None
        self._sp_id: str | None = None
        self._premise_id: str | None = None
        self._premise_address: str | None = None
        # Solar details (production meter = separate SASP with its own SP)
        self._is_solar: bool = False
        self._meter_number: str | None = None
        self._rate_plan_code: str | None = None
        self._sa_start: str | None = None
        self._prod_meter: dict | None = None

    @property
    def is_solar(self) -> bool:
        """Whether the account has solar (per the isSolar SASP flag)."""
        return self._is_solar

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def authenticate(self) -> None:
        """Login to APS and obtain B2C access token + account/SASP details.

        Flow (reverse-engineered from aps-apscom.js + apsconsumerapp APK):
        1. POST UserAuthentication with RSA-encrypted password
        2. GET the redirectUrl to establish session cookies
        3. GET GetAllUserDetails → B2C_AccessToken + account/SASP data
        """
        encrypted_pw = _encrypt_password(self._password)
        _LOGGER.debug("APS: Authenticating user %s", self._username)

        # Step 1: Login
        async with self._session.post(
            LOGIN_URL,
            json={"username": self._username, "password": encrypted_pw},
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/plain, */*",
                "Origin": SITECORE_BASE,
                "Referer": f"{SITECORE_BASE}/en/Authorization/Login",
                "User-Agent": _USER_AGENT,
            },
        ) as resp:
            raw = await resp.text()
            if resp.status != 200 or not raw.strip().startswith("{"):
                raise APSAuthError(
                    f"Login blocked/failed (HTTP {resp.status}). "
                    "APS may be rate-limiting — try again in a few minutes."
                )
            import json as _json

            data: dict[str, Any] = _json.loads(raw)

        if not data.get("isLoginSuccess"):
            raise APSAuthError(
                f"APS credentials rejected: {data.get('error', 'unknown')}"
            )

        redirect_url: str = data.get(
            "redirectUrl",
            f"{SITECORE_BASE}/en/Residential/Account/Overview/Dashboard",
        )

        # Step 2: Follow redirect to establish server-side session
        async with self._session.get(
            redirect_url,
            headers={
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
                "Referer": f"{SITECORE_BASE}/en/Authorization/Login",
                "User-Agent": _USER_AGENT,
            },
            allow_redirects=True,
        ) as resp:
            _LOGGER.debug("APS: Session established (HTTP %s)", resp.status)

        # Step 3: Get all user details (token + SASP data)
        await self._fetch_user_details()

    async def _fetch_user_details(self) -> None:
        """Call GetAllUserDetails and extract token, account ID, and active SASP."""
        async with self._session.get(
            USER_DETAILS_URL,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/plain, */*",
                "Referer": f"{SITECORE_BASE}/en/Residential/Account/Overview/Dashboard",
                "User-Agent": _USER_AGENT,
            },
        ) as resp:
            raw = await resp.text()
            _LOGGER.debug(
                "APS: GetAllUserDetails HTTP %s len=%d", resp.status, len(raw)
            )

        if not raw or not raw.strip():
            raise APSAuthError("GetAllUserDetails returned empty response.")

        import json as _json

        try:
            full: dict[str, Any] = _json.loads(raw.lstrip("\ufeff"))
        except _json.JSONDecodeError as err:
            raise APSAuthError(f"GetAllUserDetails not JSON: {raw[:200]}") from err

        details = full.get("Details", {})
        profile = details.get("profileData", {})

        if not profile:
            raise APSAuthError(
                "profileData missing from GetAllUserDetails. "
                "Keys: " + str(list(details.keys()))
            )

        token: str = profile.get("B2C_AccessToken", "")
        if not token:
            raise APSAuthError(
                "B2C_AccessToken not in profileData. Keys: " + str(list(profile.keys()))
            )
        if token.lower().startswith("bearer "):
            token = token[7:]

        self._b2c_access_token = token
        self._token_expiry = datetime.now() + timedelta(minutes=55)
        self._account_id = profile.get("AccountID")
        self._email = profile.get("emailAddress", "")

        # Extract the active SASP (service agreement / service point).
        self._sa_id = None
        self._sp_id = None
        self._premise_id = None
        self._premise_address = None

        candidates = _extract_sasp_candidates(details)
        chosen = _choose_sasp(candidates)
        if chosen:
            self._sa_id = chosen["sa_id"]
            self._sp_id = chosen["sp_id"] or None
            self._premise_id = chosen["premise_id"] or None
            self._premise_address = chosen["premise_address"]
            self._meter_number = chosen["meter"] or None
            self._rate_plan_code = chosen["rate_plan_code"] or None
            self._sa_start = chosen["sa_start"] or None
            self._is_solar = (
                chosen["is_solar"]
                or any(
                    c["is_solar"] or c["sp_type"].upper() == "PROD"
                    for c in candidates
                )
                or _payload_flags_solar(details)
            )
            self._prod_meter = (
                _find_production_meter(candidates, chosen)
                if self._is_solar
                else None
            )
            _LOGGER.debug(
                "APS: Selected SASP — SA=%s SP=%s premise=%s type=%s solar=%s "
                "prod_meter=%s (%d candidate(s) found)",
                self._sa_id,
                self._sp_id,
                self._premise_id,
                chosen["sa_type"],
                self._is_solar,
                bool(self._prod_meter),
                len(candidates),
            )
        else:
            _LOGGER.warning(
                "APS: No service agreement (SASP) found in GetAllUserDetails; "
                "usage fetching will fail. Please report this response "
                "structure (keys only, no personal data) at "
                "https://github.com/Conexo-Casa/aps-csv-ha/issues: %s",
                _describe_structure(details),
            )

        _LOGGER.debug(
            "APS: Authenticated — account=%s sa=%s sp=%s",
            self._account_id,
            self._sa_id,
            self._sp_id,
        )

    async def _ensure_authenticated(self) -> None:
        """Ensure token is valid, refreshing if needed."""
        if self._b2c_access_token is None:
            await self.authenticate()
            return
        if self._token_expiry and datetime.now() >= self._token_expiry:
            _LOGGER.debug("APS: Token expired, refreshing.")
            try:
                await self._fetch_user_details()
            except APSAuthError:
                _LOGGER.warning("APS: Refresh failed, re-authenticating.")
                await self.authenticate()

    # ------------------------------------------------------------------
    # Data fetching
    # ------------------------------------------------------------------

    async def get_daily_usage(self, days: int = 60) -> APSUsageData:
        """Fetch daily kWh usage from mobi.aps.com/ccb-billing/v1/getdailyusagecharges.

        This endpoint is discovered from Accounts/Dashboard.js. It returns per-day
        usage broken down by on-peak, off-peak, and total kWh.

        Args:
            days: Number of days of history to fetch (default 60).

        Returns:
            APSUsageData with daily series and billing cycle info.
        """
        await self._ensure_authenticated()

        if not self._sa_id or not self._sp_id:
            raise APSAuthError(
                "No active service agreement found. "
                "Cannot fetch usage data without a valid SA/SP. "
                "Check the Home Assistant log for an 'APS: No service "
                "agreement (SASP) found' warning and report it at "
                "https://github.com/Conexo-Casa/aps-csv-ha/issues"
            )

        end_dt = datetime.now()
        start_dt = end_dt - timedelta(days=days)

        params = {
            "action": "read",
            "accountNumber": self._account_id or "",
            "userName": self._username,
            "emailAddress": self._email or "",
            "sAID": self._sa_id,
            "spId": self._sp_id,
            "startDate": start_dt.strftime("%Y-%m-%d"),
            "endDate": end_dt.strftime("%Y-%m-%d"),
            "cSSUser": CSS_USER,
        }

        url = f"{DAILY_USAGE_URL}?{urllib.parse.urlencode(params)}"

        _LOGGER.debug("APS: Fetching daily usage SA=%s SP=%s", self._sa_id, self._sp_id)

        headers = {
            "Accept": "application/json, text/plain, */*",
            "Authorization": f"Bearer {self._b2c_access_token}",
            "Ocp-Apim-Subscription-Key": OCP_APIM_KEY,
            "Origin": SITECORE_BASE,
            "Referer": f"{SITECORE_BASE}/en/Residential/Account/Overview/Dashboard",
            "User-Agent": _USER_AGENT,
        }

        try:
            async with self._session.get(url, headers=headers) as resp:
                if resp.status == 401:
                    _LOGGER.warning("APS: 401 on usage GET — re-authenticating.")
                    await self.authenticate()
                    headers["Authorization"] = f"Bearer {self._b2c_access_token}"
                    async with self._session.get(url, headers=headers) as retry:
                        retry.raise_for_status()
                        data = await retry.json(content_type=None)
                elif resp.status != 200:
                    body = await resp.text()
                    raise Exception(
                        f"Usage API returned HTTP {resp.status}: {body[:200]}"
                    )
                else:
                    data = await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            raise Exception(f"Connection error fetching usage: {err}") from err

        return APSUsageData(
            series=data.get("series", []),
            bill_cycle_dates=data.get("billCycleDates", []),
            account_id=self._account_id or "",
            sa_id=self._sa_id,
            sp_id=self._sp_id,
            premise_id=self._premise_id or "",
            premise_address=self._premise_address or "",
        )

    async def get_solar_usage(self, days: int = 60) -> APSSolarData | None:
        """Fetch solar generation/export data (solar accounts only).

        POST /customerhistoryservices/v1/getsummarizedusagesolardata — the
        endpoint the APS dashboard uses when the selected service agreement
        has isSolar=true. Requires both the utility meter and the separate
        production meter (its own SASP/SP).

        Best-effort: returns None (with a log entry) instead of raising, so
        a solar endpoint hiccup never breaks the core usage sensors.
        """
        await self._ensure_authenticated()

        if not self._is_solar:
            return None
        if not self._prod_meter or not self._meter_number:
            _LOGGER.warning(
                "APS: Account is flagged solar but no production meter was "
                "found (utility meter present: %s). Please report at "
                "https://github.com/damianharouff/aps-csv-ha/issues",
                bool(self._meter_number),
            )
            return None

        end_dt = datetime.now()
        start_dt = end_dt - timedelta(days=days)
        start_str = start_dt.strftime("%Y-%m-%d")
        end_str = end_dt.strftime("%Y-%m-%d")

        payload = {
            "accountId": self._account_id or "",
            "cssUser": CSS_USER,
            "userName": self._username,
            "billCycleStartDate": start_str,
            "billCycleEndDate": end_str,
            "utilityMeterNumber": self._meter_number,
            "utilityMeterSPId": self._sp_id,
            "prodMeterNumber": self._prod_meter["meter"],
            "prodMeterSPId": self._prod_meter["sp_id"],
            "saId": self._sa_id,
            "premiseId": self._premise_id or "",
            "displayType": "D",
            "ratePlan": [
                {
                    "servicePlan": self._rate_plan_code or "",
                    "startDate": self._sa_start or start_str,
                    "endDate": end_str,
                }
            ],
        }

        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._b2c_access_token}",
            "Ocp-Apim-Subscription-Key": OCP_APIM_KEY,
            "Origin": SITECORE_BASE,
            "Referer": f"{SITECORE_BASE}/en/Residential/Account/Overview/Dashboard",
            "User-Agent": _USER_AGENT,
        }

        _LOGGER.debug(
            "APS: Fetching solar usage (utility meter + production meter)"
        )

        try:
            async with self._session.post(
                SOLAR_USAGE_URL, json=payload, headers=headers
            ) as resp:
                if resp.status == 401:
                    _LOGGER.warning("APS: 401 on solar POST — re-authenticating.")
                    await self.authenticate()
                    headers["Authorization"] = f"Bearer {self._b2c_access_token}"
                    async with self._session.post(
                        SOLAR_USAGE_URL, json=payload, headers=headers
                    ) as retry:
                        if retry.status != 200:
                            body = await retry.text()
                            _LOGGER.warning(
                                "APS: Solar usage API HTTP %s: %s",
                                retry.status,
                                body[:200],
                            )
                            return None
                        data = await retry.json(content_type=None)
                elif resp.status != 200:
                    body = await resp.text()
                    _LOGGER.warning(
                        "APS: Solar usage API HTTP %s: %s", resp.status, body[:200]
                    )
                    return None
                else:
                    data = await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            _LOGGER.warning("APS: Connection error fetching solar usage: %s", err)
            return None

        series = _as_list(_ci_get(data, "Series"))
        if not series:
            _LOGGER.warning(
                "APS: Solar usage response had no Series. Keys: %s",
                sorted(data.keys()) if isinstance(data, dict) else type(data).__name__,
            )
            return None

        return APSSolarData(series)

    async def get_financial_data(self) -> dict[str, Any]:
        """Return financial data extracted from GetAllUserDetails.

        This data is always available after authentication — no extra API call needed.
        Returns current bill amount, due date, last payment, autopay status.
        """
        await self._ensure_authenticated()
        # Re-fetch to get fresh financial data
        async with self._session.get(
            USER_DETAILS_URL,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/plain, */*",
                "Referer": f"{SITECORE_BASE}/en/Residential/Account/Overview/Dashboard",
                "User-Agent": _USER_AGENT,
            },
        ) as resp:
            raw = await resp.text()

        import json as _json

        try:
            full = _json.loads(raw.lstrip("\ufeff"))
        except _json.JSONDecodeError:
            return {}

        details = full.get("Details", {})
        profile = details.get("profileData", {})
        fin = (
            details.get("AccountDetails", {})
            .get("getAccountDetailsResponse", {})
            .get("getAccountDetailsRes", {})
            .get("getAccountFinancialDetails", {})
        )

        return {
            "account_id": profile.get("AccountID", ""),
            "outstanding_balance": fin.get(
                "currentBalance", profile.get("OutstandingBillAmount")
            ),
            "due_date": fin.get("dueDt", profile.get("DueDate")),
            "last_payment_amount": fin.get("lastPayAmt"),
            "last_payment_date": fin.get("lastPayDt"),
            "auto_pay": profile.get("autoPay") == "Y",
            "budget_billing": profile.get("isEnrolledInBudget") == "Y",
            "new_charges": fin.get("newCharges"),
            "premise_address": self._premise_address or "",
            "rate_plan": None,  # filled by coordinator after usage fetch
        }

    async def get_account_id(self) -> str | None:
        """Return the account ID after authentication."""
        if self._account_id is None:
            await self._ensure_authenticated()
        return self._account_id
