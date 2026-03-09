import os
import json
import time
from datetime import datetime, timedelta
from flask import Flask, render_template_string, request, jsonify
from dotenv import load_dotenv
from sp_api.api import Inventories, Sales
from sp_api.base import Marketplaces, SellingApiException
from sp_api.base.exceptions import SellingApiRequestThrottledException
from sp_api.base.sales_enum import Granularity

load_dotenv()
app = Flask(__name__)

SAFETY_STOCK_DAYS = 90

SHIPPING = {
    "AIR":   {"lead_days": 10, "min_duration": {"EU": 30, "UK": 30, "US": 30, "CA": 30}},
    "TRUCK": {"lead_days": 20, "min_duration": {"EU": 50, "UK": 50}},
    "SEA":   {"lead_days": 45, "min_duration": {"EU": 70, "UK": 70, "US": 45, "CA": 45}},
}

EU_CREDENTIALS = {
    "refresh_token": os.getenv("EU_REFRESH_TOKEN"),
    "lwa_app_id": os.getenv("EU_LWA_CLIENT_ID"),
    "lwa_client_secret": os.getenv("EU_LWA_CLIENT_SECRET"),
}
NA_CREDENTIALS = {
    "refresh_token": os.getenv("NA_REFRESH_TOKEN"),
    "lwa_app_id": os.getenv("NA_LWA_CLIENT_ID"),
    "lwa_client_secret": os.getenv("NA_LWA_CLIENT_SECRET"),
}

WAREHOUSES = {
    "EU": {
        "credentials": EU_CREDENTIALS,
        "inv_marketplace": Marketplaces.DE,
        "inv_granularity_id": "A1PA6795UKMFR9",
        "sales_marketplaces": {
            "DE": Marketplaces.DE, "FR": Marketplaces.FR, "IT": Marketplaces.IT,
            "ES": Marketplaces.ES, "NL": Marketplaces.NL, "PL": Marketplaces.PL,
            "SE": Marketplaces.SE, "BE": Marketplaces.BE,
        },
        "label": "AMZ EU",
        "color": "#3b82f6",
    },
    "UK": {
        "credentials": EU_CREDENTIALS,
        "inv_marketplace": Marketplaces.GB,
        "inv_granularity_id": "A1F83G8C2ARO7P",
        "sales_marketplaces": {"UK": Marketplaces.GB},
        "label": "AMZ UK",
        "color": "#8b5cf6",
    },
    "US": {
        "credentials": NA_CREDENTIALS,
        "inv_marketplace": Marketplaces.US,
        "inv_granularity_id": "ATVPDKIKX0DER",
        "sales_marketplaces": {"US": Marketplaces.US},
        "label": "AMZ US",
        "color": "#f59e0b",
    },
    "CA": {
        "credentials": NA_CREDENTIALS,
        "inv_marketplace": Marketplaces.CA,
        "inv_granularity_id": "A2EUQ1WTGCTBG2",
        "sales_marketplaces": {"CA": Marketplaces.CA},
        "label": "AMZ CA",
        "color": "#ef4444",
    },
}


# ── throttle-safe API call ────────────────────────────────────────────

def api_call_with_retry(fn, max_retries=3):
    """Call an API function, retry on throttle with exponential backoff."""
    for attempt in range(max_retries):
        try:
            return fn()
        except SellingApiRequestThrottledException:
            wait = 2 ** (attempt + 1)
            print(f"  Throttled, waiting {wait}s...")
            time.sleep(wait)
    return fn()


# ── helpers ──────────────────────────────────────────────────────────

def _parse_inv_item(item):
    d = item.get("inventoryDetails", {})
    return {
        "asin": item["asin"],
        "fulfillable": d.get("fulfillableQuantity", 0),
        "inbound_working": d.get("inboundWorkingQuantity", 0),
        "inbound_shipped": d.get("inboundShippedQuantity", 0),
        "inbound_receiving": d.get("inboundReceivingQuantity", 0),
        "reserved": d.get("reservedQuantity", {}).get("totalReservedQuantity", 0),
        "total": item.get("totalQuantity", 0),
        "product_name": item.get("productName", ""),
        "sku": item.get("sellerSku", ""),
    }


def get_inventory(credentials, marketplace, granularity_id, asin):
    inv_client = Inventories(credentials=credentials, marketplace=marketplace)
    next_token = None
    page = 0
    while page < 30:
        page += 1
        kwargs = {"details": True, "granularityType": "Marketplace", "granularityId": granularity_id}
        if next_token:
            kwargs["nextToken"] = next_token
        resp = api_call_with_retry(lambda: inv_client.get_inventory_summary_marketplace(**kwargs))
        for item in resp.payload.get("inventorySummaries", []):
            if item.get("asin") == asin and item.get("condition") == "NewItem":
                return _parse_inv_item(item)
        next_token = resp.payload.get("nextToken") or resp.next_token
        if not next_token:
            break
    return None


def get_all_inventory(credentials, marketplace, granularity_id):
    """Get ALL NewItem inventory for a warehouse."""
    inv_client = Inventories(credentials=credentials, marketplace=marketplace)
    next_token = None
    page = 0
    items = {}
    while page < 30:
        page += 1
        kwargs = {"details": True, "granularityType": "Marketplace", "granularityId": granularity_id}
        if next_token:
            kwargs["nextToken"] = next_token
        resp = api_call_with_retry(lambda: inv_client.get_inventory_summary_marketplace(**kwargs))
        for item in resp.payload.get("inventorySummaries", []):
            if item.get("condition") == "NewItem":
                parsed = _parse_inv_item(item)
                parsed["product_name"] = parsed["product_name"][:60]
                items[parsed["asin"]] = parsed
        next_token = resp.payload.get("nextToken") or resp.next_token
        if not next_token:
            break
    return items


def get_sales_90d(credentials, marketplace, asin):
    end = datetime.utcnow()
    start = end - timedelta(days=90)
    interval = (start.strftime("%Y-%m-%dT00:00:00Z"), end.strftime("%Y-%m-%dT00:00:00Z"))
    try:
        client = Sales(credentials=credentials, marketplace=marketplace)
        resp = api_call_with_retry(
            lambda: client.get_order_metrics(interval=interval, granularity=Granularity.TOTAL, asin=[asin])
        )
        if resp.payload:
            return resp.payload[0].get("unitCount", 0)
    except Exception:
        pass
    return 0


def scan_all_skus():
    all_items = {}
    for wh_key, wh in WAREHOUSES.items():
        print(f"  Scanning {wh_key}...")
        inv_client = Inventories(credentials=wh["credentials"], marketplace=wh["inv_marketplace"])
        next_token = None
        page = 0
        while page < 30:
            page += 1
            kwargs = {"details": True, "granularityType": "Marketplace", "granularityId": wh["inv_granularity_id"]}
            if next_token:
                kwargs["nextToken"] = next_token
            resp = api_call_with_retry(lambda: inv_client.get_inventory_summary_marketplace(**kwargs))
            for item in resp.payload.get("inventorySummaries", []):
                if item.get("condition") == "NewItem":
                    asin = item["asin"]
                    if asin not in all_items:
                        all_items[asin] = {
                            "asin": asin,
                            "sku": item["sellerSku"],
                            "name": item.get("productName", "")[:80],
                        }
            next_token = resp.payload.get("nextToken") or resp.next_token
            if not next_token:
                break
        time.sleep(1)  # small pause between warehouses
    return all_items


def compute_shipping_plan(wh_key, velocity, stock, transit, plan):
    total_pipeline = stock + transit + plan
    days_left = round(stock / velocity, 1) if velocity > 0 else float("inf")
    duration = round(total_pipeline / velocity, 1) if velocity > 0 else float("inf")

    methods = []
    for method_name in ["AIR", "TRUCK", "SEA"]:
        method = SHIPPING[method_name]
        if wh_key not in method["min_duration"]:
            continue
        min_dur = method["min_duration"][wh_key]
        lead = method["lead_days"]
        target_coverage = SAFETY_STOCK_DAYS + lead
        units_needed = max(0, round(velocity * target_coverage - total_pipeline))
        days_to_act = round(days_left - min_dur, 1) if days_left != float("inf") else float("inf")
        urgent = days_to_act != float("inf") and days_to_act <= 0
        methods.append({
            "method": method_name,
            "lead_days": lead,
            "min_duration": min_dur,
            "units_needed": units_needed,
            "days_to_act": days_to_act,
            "urgent": urgent,
        })
    return {
        "stock": stock, "transit": transit, "plan": plan,
        "total_pipeline": total_pipeline, "days_left": days_left,
        "duration": duration, "methods": methods,
    }


def fetch_data_for_asin(asin):
    result = {}
    product_name = ""
    sku = ""
    for wh_key, wh in WAREHOUSES.items():
        inv = get_inventory(wh["credentials"], wh["inv_marketplace"], wh["inv_granularity_id"], asin)
        total_sales = 0
        mp_breakdown = {}
        for mp_key, mp_val in wh["sales_marketplaces"].items():
            units = get_sales_90d(wh["credentials"], mp_val, asin)
            mp_breakdown[mp_key] = units
            total_sales += units
        velocity = round(total_sales / 90, 2)
        if inv:
            stock = inv["fulfillable"]
            transit = inv["inbound_shipped"] + inv["inbound_receiving"]
            plan = inv["inbound_working"]
            if not product_name:
                product_name = inv["product_name"]
                sku = inv["sku"]
        else:
            stock = transit = plan = 0
        shipping = compute_shipping_plan(wh_key, velocity, stock, transit, plan)
        shipping["sales_90d"] = total_sales
        shipping["velocity"] = velocity
        shipping["mp_breakdown"] = mp_breakdown
        result[wh_key] = shipping
    return result, product_name, sku


def fetch_shipment_plan(wh_key):
    """Fetch ALL ASINs for a warehouse, compute replenishment, group by shipping method."""
    wh = WAREHOUSES[wh_key]
    all_inv = get_all_inventory(wh["credentials"], wh["inv_marketplace"], wh["inv_granularity_id"])

    shipments = {"AIR": [], "TRUCK": [], "SEA": []}
    for asin, inv in all_inv.items():
        total_sales = 0
        for mp_key, mp_val in wh["sales_marketplaces"].items():
            total_sales += get_sales_90d(wh["credentials"], mp_val, asin)
        velocity = round(total_sales / 90, 2)
        stock = inv["fulfillable"]
        transit = inv["inbound_shipped"] + inv["inbound_receiving"]
        plan = inv["inbound_working"]
        sp = compute_shipping_plan(wh_key, velocity, stock, transit, plan)

        for m in sp["methods"]:
            if m["units_needed"] > 0:
                shipments[m["method"]].append({
                    "asin": asin,
                    "sku": inv["sku"],
                    "product": inv["product_name"],
                    "stock": stock,
                    "transit": transit,
                    "plan": plan,
                    "pipeline": sp["total_pipeline"],
                    "velocity": velocity,
                    "sales_90d": total_sales,
                    "duration": sp["duration"],
                    "days_left": sp["days_left"],
                    "units_needed": m["units_needed"],
                    "days_to_act": m["days_to_act"],
                    "urgent": m["urgent"],
                })

    # Sort each group: urgent first, then by days_to_act ascending
    for method in shipments:
        shipments[method].sort(key=lambda x: (
            0 if x["urgent"] else 1,
            x["days_to_act"] if x["days_to_act"] != float("inf") else 99999,
        ))
    return shipments


# ── startup scan ─────────────────────────────────────────────────────

print("Scanning all warehouses for SKU list...")
SKU_CACHE = scan_all_skus()
print(f"Found {len(SKU_CACHE)} unique ASINs")


# ── shared CSS + nav ─────────────────────────────────────────────────

SHARED_CSS = """
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0f1117; color: #e0e0e0; padding: 20px; }
h1 { text-align: center; margin-bottom: 4px; color: #fff; font-size: 26px; letter-spacing: 2px; }
.subtitle { text-align: center; color: #6b7280; margin-bottom: 16px; font-size: 13px; }

.nav { display: flex; justify-content: center; gap: 4px; margin-bottom: 20px; }
.nav a { padding: 8px 24px; border-radius: 6px; text-decoration: none; font-size: 13px; font-weight: 600; color: #9ca3af; background: #1a1d27; border: 1px solid #2d3040; transition: all .15s; }
.nav a:hover { color: #e5e7eb; border-color: #3b82f6; }
.nav a.active { background: #3b82f6; color: #fff; border-color: #3b82f6; }

.controls { display: flex; justify-content: center; align-items: center; gap: 16px; margin-bottom: 20px; flex-wrap: wrap; }
.controls label { color: #9ca3af; font-size: 13px; font-weight: 600; }
select { background: #1a1d27; color: #e0e0e0; border: 1px solid #2d3040; border-radius: 6px; padding: 8px 14px; font-size: 14px; cursor: pointer; }
select:focus { outline: none; border-color: #3b82f6; }
.btn { background: #3b82f6; color: #fff; border: none; border-radius: 6px; padding: 8px 20px; font-size: 14px; cursor: pointer; font-weight: 600; }
.btn:hover { background: #2563eb; }
.btn:disabled { background: #374151; cursor: wait; }

.params { display: flex; justify-content: center; gap: 10px; margin-bottom: 20px; flex-wrap: wrap; }
.params span { background: #1a1d27; padding: 5px 12px; border-radius: 5px; font-size: 12px; color: #6b7280; }
.params span b { color: #60a5fa; }

.timestamp { text-align: center; color: #374151; font-size: 11px; margin-top: 16px; }
.no-data { text-align: center; color: #4b5563; padding: 40px; font-size: 14px; }
.loading { text-align: center; padding: 60px; color: #6b7280; font-size: 16px; }
.loading .spinner { display: inline-block; width: 24px; height: 24px; border: 3px solid #374151; border-top-color: #3b82f6; border-radius: 50%; animation: spin 0.8s linear infinite; margin-right: 10px; vertical-align: middle; }
@keyframes spin { to { transform: rotate(360deg); } }
"""

NAV_HTML = """
<div class="nav">
    <a href="/" class="{{ 'active' if page == 'sku' else '' }}">SKU View</a>
    <a href="/shipments" class="{{ 'active' if page == 'shipments' else '' }}">Shipment Planner</a>
</div>
"""


# ── SKU view template ────────────────────────────────────────────────

SKU_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>PRETTY 2.0 - SKU View</title>
    <style>
        """ + SHARED_CSS + """
        .product-info { text-align: center; margin-bottom: 20px; }
        .product-info .name { font-size: 16px; color: #e5e7eb; font-weight: 600; }
        .product-info .asin { font-family: monospace; color: #6b7280; font-size: 13px; }
        .grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 24px; }
        @media (max-width: 1200px) { .grid { grid-template-columns: repeat(2, 1fr); } }
        @media (max-width: 700px) { .grid { grid-template-columns: 1fr; } }
        .wh-card { background: #1a1d27; border-radius: 10px; padding: 18px; border-top: 3px solid #374151; }
        .wh-card.wh-EU { border-top-color: #3b82f6; }
        .wh-card.wh-UK { border-top-color: #8b5cf6; }
        .wh-card.wh-US { border-top-color: #f59e0b; }
        .wh-card.wh-CA { border-top-color: #ef4444; }
        .wh-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px; }
        .wh-name { font-size: 16px; font-weight: 700; }
        .wh-EU .wh-name { color: #3b82f6; }
        .wh-UK .wh-name { color: #8b5cf6; }
        .wh-US .wh-name { color: #f59e0b; }
        .wh-CA .wh-name { color: #ef4444; }
        .wh-vel { font-size: 22px; font-weight: 700; color: #fff; }
        .wh-vel small { font-size: 11px; color: #6b7280; font-weight: 400; }
        .metrics { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin-bottom: 14px; }
        .metric { background: #0f1117; border-radius: 6px; padding: 10px 8px; text-align: center; }
        .metric .val { font-size: 18px; font-weight: 700; color: #e5e7eb; }
        .metric .lbl { font-size: 10px; color: #6b7280; text-transform: uppercase; letter-spacing: 0.5px; margin-top: 2px; }
        .metric.stock .val { color: #10b981; }
        .metric.transit .val { color: #60a5fa; }
        .metric.plan .val { color: #a78bfa; }
        .duration-row { display: flex; justify-content: space-between; background: #0f1117; border-radius: 6px; padding: 10px 12px; margin-bottom: 10px; }
        .duration-row .dur-label { font-size: 11px; color: #6b7280; }
        .duration-row .dur-val { font-size: 16px; font-weight: 700; }
        .dur-ok { color: #10b981; }
        .dur-warn { color: #f59e0b; }
        .dur-danger { color: #ef4444; }
        .dur-inf { color: #374151; }
        .shipping-methods { margin-top: 12px; }
        .ship-title { font-size: 11px; color: #6b7280; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 8px; font-weight: 600; }
        .ship-row { display: flex; justify-content: space-between; align-items: center; padding: 8px 10px; border-radius: 6px; margin-bottom: 4px; font-size: 13px; }
        .ship-row.air { background: #1e3a5f22; border-left: 3px solid #60a5fa; }
        .ship-row.truck { background: #3f2d0a22; border-left: 3px solid #f59e0b; }
        .ship-row.sea { background: #064e3b22; border-left: 3px solid #10b981; }
        .ship-method { font-weight: 700; width: 55px; }
        .ship-row.air .ship-method { color: #60a5fa; }
        .ship-row.truck .ship-method { color: #f59e0b; }
        .ship-row.sea .ship-method { color: #10b981; }
        .ship-units { font-weight: 600; color: #e5e7eb; }
        .ship-action { font-size: 11px; padding: 3px 8px; border-radius: 4px; font-weight: 600; }
        .action-now { background: #7f1d1d; color: #fca5a5; }
        .action-soon { background: #78350f; color: #fcd34d; }
        .action-ok { background: #064e3b; color: #6ee7b7; }
        .action-na { background: #1f2937; color: #4b5563; }
        .mp-breakdown { font-size: 11px; color: #4b5563; margin-top: 8px; text-align: center; }
    </style>
</head>
<body>
    <h1>PRETTY 2.0</h1>
    <p class="subtitle">Replenishment Dashboard</p>
    """ + NAV_HTML + """
    <div class="controls">
        <label>SKU / ASIN:</label>
        <select id="skuSelect" onchange="loadData()" style="min-width:400px;">
            <option value="">-- Select a product --</option>
            {% for item in sku_list %}
            <option value="{{ item.asin }}" {% if item.asin == selected_asin %}selected{% endif %}>
                {{ item.sku }} | {{ item.asin }} | {{ item.name }}
            </option>
            {% endfor %}
        </select>
        <button class="btn" onclick="loadData()" id="refreshBtn">Refresh</button>
    </div>
    <div class="params">
        <span>Safety Stock: <b>{{ safety_days }}d</b></span>
        <span>AIR: <b>30d all WHs</b></span>
        <span>TRUCK: <b>50d EU/UK</b></span>
        <span>SEA: <b>45d US/CA, 70d EU/UK</b></span>
    </div>
    <div id="content">
        {% if data %}
        <div class="product-info">
            <div class="name">{{ product_name }}</div>
            <div class="asin">{{ selected_asin }} &middot; {{ sku }}</div>
        </div>
        <div class="grid">
            {% for wh_key in ["EU", "UK", "US", "CA"] %}
            {% set wh = data[wh_key] %}
            <div class="wh-card wh-{{ wh_key }}">
                <div class="wh-header">
                    <div class="wh-name">{{ wh_labels[wh_key] }}</div>
                    <div class="wh-vel">{{ wh.velocity }} <small>units/day</small></div>
                </div>
                <div class="metrics">
                    <div class="metric stock"><div class="val">{{ wh.stock }}</div><div class="lbl">Stock</div></div>
                    <div class="metric transit"><div class="val">{{ wh.transit }}</div><div class="lbl">Transit</div></div>
                    <div class="metric plan"><div class="val">{{ wh.plan }}</div><div class="lbl">Plan</div></div>
                </div>
                <div class="duration-row">
                    <div>
                        <div class="dur-label">Duration</div>
                        <div class="dur-val {% if wh.duration == 'inf' %}dur-inf{% elif wh.duration >= 90 %}dur-ok{% elif wh.duration >= 45 %}dur-warn{% else %}dur-danger{% endif %}">
                            {% if wh.duration == 'inf' %}&infin;{% else %}{{ wh.duration }}d{% endif %}
                        </div>
                    </div>
                    <div style="text-align:right;">
                        <div class="dur-label">Days Left</div>
                        <div class="dur-val {% if wh.days_left == 'inf' %}dur-inf{% elif wh.days_left >= 90 %}dur-ok{% elif wh.days_left >= 45 %}dur-warn{% else %}dur-danger{% endif %}">
                            {% if wh.days_left == 'inf' %}&infin;{% else %}{{ wh.days_left }}d{% endif %}
                        </div>
                    </div>
                </div>
                <div class="shipping-methods">
                    <div class="ship-title">Replenishment</div>
                    {% for m in wh.methods %}
                    <div class="ship-row {{ m.method|lower }}">
                        <span class="ship-method">{{ m.method }}</span>
                        <span style="font-size:11px; color:#6b7280;">min {{ m.min_duration }}d</span>
                        <span class="ship-units">{{ m.units_needed }} units</span>
                        <span class="ship-action {% if m.days_to_act == 'inf' %}action-na{% elif m.urgent %}action-now{% elif m.days_to_act <= 14 %}action-soon{% else %}action-ok{% endif %}">
                            {% if m.days_to_act == 'inf' %}N/A{% elif m.urgent %}SEND NOW{% else %}{{ m.days_to_act }}d left{% endif %}
                        </span>
                    </div>
                    {% endfor %}
                </div>
                <div class="mp-breakdown">
                    Sales 90d: {{ wh.sales_90d }} &middot;
                    {% for mp, cnt in wh.mp_breakdown.items() %}{{ mp }}:{{ cnt }}{% if not loop.last %}, {% endif %}{% endfor %}
                </div>
            </div>
            {% endfor %}
        </div>
        {% else %}
        <div class="no-data">Select a product from the dropdown to view replenishment data.</div>
        {% endif %}
    </div>
    <p class="timestamp">Last refreshed: {{ timestamp }}</p>
    <script>
        function loadData() {
            var asin = document.getElementById('skuSelect').value;
            if (!asin) return;
            document.getElementById('refreshBtn').disabled = true;
            document.getElementById('refreshBtn').textContent = 'Loading...';
            document.getElementById('content').innerHTML = '<div class="loading"><span class="spinner"></span>Fetching from Amazon APIs... (30-60s)</div>';
            window.location.href = '/?asin=' + asin;
        }
    </script>
</body>
</html>
"""


# ── Shipment Planner template ────────────────────────────────────────

SHIPMENT_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>PRETTY 2.0 - Shipment Planner</title>
    <style>
        """ + SHARED_CSS + """
        .wh-selector { display: flex; justify-content: center; gap: 8px; margin-bottom: 20px; }
        .wh-btn { padding: 10px 28px; border-radius: 8px; text-decoration: none; font-size: 14px; font-weight: 700; border: 2px solid #2d3040; background: #1a1d27; color: #6b7280; transition: all .15s; }
        .wh-btn:hover { border-color: #4b5563; color: #e5e7eb; }
        .wh-btn.sel-EU { border-color: #3b82f6; background: #1e3a5f; color: #60a5fa; }
        .wh-btn.sel-UK { border-color: #8b5cf6; background: #2e1065; color: #a78bfa; }
        .wh-btn.sel-US { border-color: #f59e0b; background: #451a03; color: #fcd34d; }
        .wh-btn.sel-CA { border-color: #ef4444; background: #450a0a; color: #fca5a5; }

        .channel-section { margin-bottom: 28px; }
        .channel-header { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; padding-bottom: 8px; border-bottom: 2px solid #1e2130; }
        .channel-icon { width: 36px; height: 36px; border-radius: 8px; display: flex; align-items: center; justify-content: center; font-weight: 900; font-size: 13px; }
        .channel-icon.air { background: #1e3a5f; color: #60a5fa; }
        .channel-icon.truck { background: #451a03; color: #fcd34d; }
        .channel-icon.sea { background: #064e3b; color: #6ee7b7; }
        .channel-name { font-size: 18px; font-weight: 700; }
        .channel-name.air { color: #60a5fa; }
        .channel-name.truck { color: #f59e0b; }
        .channel-name.sea { color: #10b981; }
        .channel-count { font-size: 13px; color: #6b7280; }
        .channel-total { margin-left: auto; font-size: 14px; font-weight: 600; color: #9ca3af; }
        .channel-total b { color: #e5e7eb; font-size: 18px; }

        table { width: 100%; border-collapse: collapse; }
        th { background: #161822; color: #6b7280; font-weight: 600; text-transform: uppercase; font-size: 10px; letter-spacing: 0.5px; padding: 10px 10px; text-align: center; border-bottom: 2px solid #2d3040; }
        th:first-child, td:first-child { text-align: left; }
        td { padding: 10px 10px; text-align: center; border-bottom: 1px solid #1a1d27; font-size: 13px; }
        tr:hover { background: #1a1d2799; }
        .sku-cell { font-weight: 600; color: #e5e7eb; font-size: 12px; }
        .asin-cell { font-family: monospace; color: #6b7280; font-size: 11px; }
        .product-cell { color: #9ca3af; font-size: 11px; max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; text-align: left; }
        .num-green { color: #10b981; font-weight: 600; }
        .num-blue { color: #60a5fa; }
        .num-purple { color: #a78bfa; }
        .num-yellow { color: #f59e0b; font-weight: 600; }
        .units-cell { font-size: 15px; font-weight: 700; color: #e5e7eb; }
        .badge-urgent { background: #7f1d1d; color: #fca5a5; padding: 3px 8px; border-radius: 4px; font-size: 11px; font-weight: 700; }
        .badge-soon { background: #78350f; color: #fcd34d; padding: 3px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }
        .badge-ok { background: #064e3b; color: #6ee7b7; padding: 3px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }
        .empty-channel { color: #374151; padding: 20px; text-align: center; font-size: 13px; }
        .summary-bar { display: flex; justify-content: center; gap: 24px; margin-bottom: 20px; flex-wrap: wrap; }
        .summary-item { background: #1a1d27; border-radius: 8px; padding: 12px 20px; text-align: center; min-width: 120px; }
        .summary-item .s-val { font-size: 22px; font-weight: 700; color: #fff; }
        .summary-item .s-lbl { font-size: 10px; color: #6b7280; text-transform: uppercase; margin-top: 2px; }
        .summary-item.air { border-top: 3px solid #60a5fa; }
        .summary-item.truck { border-top: 3px solid #f59e0b; }
        .summary-item.sea { border-top: 3px solid #10b981; }
    </style>
</head>
<body>
    <h1>PRETTY 2.0</h1>
    <p class="subtitle">Shipment Planner - All ASINs by Warehouse & Channel</p>
    """ + NAV_HTML + """

    <div class="wh-selector">
        {% for wk in ["EU", "UK", "US", "CA"] %}
        <a href="/shipments?wh={{ wk }}" class="wh-btn {% if wk == selected_wh %}sel-{{ wk }}{% endif %}">
            {{ wh_labels[wk] }}
        </a>
        {% endfor %}
    </div>

    {% if shipments %}
    <div class="summary-bar">
        {% for ch in ["AIR", "TRUCK", "SEA"] %}
        {% if shipments[ch] is defined %}
        <div class="summary-item {{ ch|lower }}">
            <div class="s-val">{{ shipments[ch]|length }}</div>
            <div class="s-lbl">{{ ch }} SKUs</div>
        </div>
        <div class="summary-item {{ ch|lower }}">
            <div class="s-val">{{ shipments[ch]|sum(attribute='units_needed') }}</div>
            <div class="s-lbl">{{ ch }} Total Units</div>
        </div>
        {% endif %}
        {% endfor %}
    </div>

    {% for ch in ["AIR", "TRUCK", "SEA"] %}
    {% if shipments[ch] is defined %}
    <div class="channel-section">
        <div class="channel-header">
            <div class="channel-icon {{ ch|lower }}">{{ ch[:1] }}</div>
            <div class="channel-name {{ ch|lower }}">{{ ch }}</div>
            <div class="channel-count">{{ shipments[ch]|length }} SKUs to ship</div>
            <div class="channel-total">Total: <b>{{ shipments[ch]|sum(attribute='units_needed') }} units</b></div>
        </div>
        {% if shipments[ch] %}
        <table>
            <thead>
                <tr>
                    <th>SKU</th>
                    <th>ASIN</th>
                    <th>Product</th>
                    <th>Stock</th>
                    <th>Transit</th>
                    <th>Plan</th>
                    <th>Velocity</th>
                    <th>Duration</th>
                    <th>Days Left</th>
                    <th>Units to Send</th>
                    <th>Action</th>
                </tr>
            </thead>
            <tbody>
                {% for r in shipments[ch] %}
                <tr>
                    <td class="sku-cell">{{ r.sku }}</td>
                    <td class="asin-cell">{{ r.asin }}</td>
                    <td class="product-cell" title="{{ r.product }}">{{ r.product }}</td>
                    <td class="num-green">{{ r.stock }}</td>
                    <td class="num-blue">{{ r.transit }}</td>
                    <td class="num-purple">{{ r.plan }}</td>
                    <td class="num-yellow">{{ r.velocity }}/d</td>
                    <td>{% if r.duration == 'inf' %}&infin;{% else %}{{ r.duration }}d{% endif %}</td>
                    <td>{% if r.days_left == 'inf' %}&infin;{% else %}{{ r.days_left }}d{% endif %}</td>
                    <td class="units-cell">{{ r.units_needed }}</td>
                    <td>
                        {% if r.urgent %}
                            <span class="badge-urgent">SEND NOW</span>
                        {% elif r.days_to_act != 'inf' and r.days_to_act <= 14 %}
                            <span class="badge-soon">{{ r.days_to_act }}d left</span>
                        {% elif r.days_to_act != 'inf' %}
                            <span class="badge-ok">{{ r.days_to_act }}d left</span>
                        {% endif %}
                    </td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
        {% else %}
        <div class="empty-channel">No ASINs need {{ ch }} shipment right now.</div>
        {% endif %}
    </div>
    {% endif %}
    {% endfor %}

    {% else %}
    <div class="no-data">Select a warehouse above to see all ASINs that need replenishment, grouped by shipping channel.</div>
    {% endif %}

    <p class="timestamp">Last refreshed: {{ timestamp }}</p>
</body>
</html>
"""


# ── routes ───────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    selected_asin = request.args.get("asin", "")
    sku_list = sorted(SKU_CACHE.values(), key=lambda x: x["sku"])
    data = None
    product_name = ""
    sku = ""
    if selected_asin:
        data, product_name, sku = fetch_data_for_asin(selected_asin)
        for wh_key in data:
            wh = data[wh_key]
            if wh["duration"] == float("inf"):
                wh["duration"] = "inf"
            if wh["days_left"] == float("inf"):
                wh["days_left"] = "inf"
            for m in wh["methods"]:
                if m["days_to_act"] == float("inf"):
                    m["days_to_act"] = "inf"
    wh_labels = {k: v["label"] for k, v in WAREHOUSES.items()}
    return render_template_string(
        SKU_TEMPLATE,
        page="sku",
        sku_list=sku_list, selected_asin=selected_asin,
        data=data, product_name=product_name[:80] if product_name else "", sku=sku,
        safety_days=SAFETY_STOCK_DAYS, wh_labels=wh_labels,
        timestamp=datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
    )


@app.route("/shipments")
def shipments():
    selected_wh = request.args.get("wh", "")
    wh_labels = {k: v["label"] for k, v in WAREHOUSES.items()}
    shipments_data = None

    if selected_wh and selected_wh in WAREHOUSES:
        shipments_data = fetch_shipment_plan(selected_wh)
        # Convert inf for template
        for method in shipments_data:
            for r in shipments_data[method]:
                if r["duration"] == float("inf"):
                    r["duration"] = "inf"
                if r["days_left"] == float("inf"):
                    r["days_left"] = "inf"
                if r["days_to_act"] == float("inf"):
                    r["days_to_act"] = "inf"

    return render_template_string(
        SHIPMENT_TEMPLATE,
        page="shipments",
        selected_wh=selected_wh,
        shipments=shipments_data,
        wh_labels=wh_labels,
        timestamp=datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
    )


if __name__ == "__main__":
    print("Starting PRETTY 2.0 Dashboard on http://127.0.0.1:8080")
    app.run(debug=False, host="0.0.0.0", port=8080)
