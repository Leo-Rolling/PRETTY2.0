import os
from dotenv import load_dotenv
from sp_api.api import Orders, Inventories, FbaInboundEligibility, FulfillmentInbound, Sales
from sp_api.base import Marketplaces, SellingApiException
load_dotenv()

# --- EU5 Account Credentials ---
EU_CREDENTIALS = {
    "refresh_token": os.getenv("EU_REFRESH_TOKEN"),
    "lwa_app_id": os.getenv("EU_LWA_CLIENT_ID"),
    "lwa_client_secret": os.getenv("EU_LWA_CLIENT_SECRET"),
}

# --- NA Account Credentials ---
NA_CREDENTIALS = {
    "refresh_token": os.getenv("NA_REFRESH_TOKEN"),
    "lwa_app_id": os.getenv("NA_LWA_CLIENT_ID"),
    "lwa_client_secret": os.getenv("NA_LWA_CLIENT_SECRET"),
}

# Marketplace mappings for each account
EU_MARKETPLACES = {
    "DE": Marketplaces.DE,
    "FR": Marketplaces.FR,
    "IT": Marketplaces.IT,
    "ES": Marketplaces.ES,
    "UK": Marketplaces.UK,
}

NA_MARKETPLACES = {
    "US": Marketplaces.US,
    "CA": Marketplaces.CA,
}


def get_eu_client(api_class, marketplace_key="DE"):
    """Create an SP-API client for the EU5 account."""
    marketplace = EU_MARKETPLACES.get(marketplace_key, Marketplaces.DE)
    return api_class(credentials=EU_CREDENTIALS, marketplace=marketplace)


def get_na_client(api_class, marketplace_key="US"):
    """Create an SP-API client for the NA account."""
    marketplace = NA_MARKETPLACES.get(marketplace_key, Marketplaces.US)
    return api_class(credentials=NA_CREDENTIALS, marketplace=marketplace)


def test_eu_connection():
    """Test the EU5 account connection by fetching orders."""
    print("Testing EU5 connection (DE marketplace)...")
    try:
        client = get_eu_client(Orders)
        response = client.get_orders(CreatedAfter="2024-01-01T00:00:00Z", MaxResultsPerPage=1)
        print(f"  EU5 Connection OK - Orders found: {len(response.payload.get('Orders', []))}")
        return True
    except SellingApiException as e:
        print(f"  EU5 Connection FAILED: {e}")
        return False


def test_na_connection():
    """Test the NA account connection by fetching orders."""
    print("Testing NA connection (US marketplace)...")
    try:
        client = get_na_client(Orders)
        response = client.get_orders(CreatedAfter="2024-01-01T00:00:00Z", MaxResultsPerPage=1)
        print(f"  NA Connection OK - Orders found: {len(response.payload.get('Orders', []))}")
        return True
    except SellingApiException as e:
        print(f"  NA Connection FAILED: {e}")
        return False


if __name__ == "__main__":
    print("=" * 50)
    print("PRETTY 2.0 - Amazon SP-API Connection Test")
    print("=" * 50)
    eu_ok = test_eu_connection()
    print()
    na_ok = test_na_connection()
    print()
    print("=" * 50)
    if eu_ok and na_ok:
        print("All connections successful!")
    else:
        print("Some connections failed. Check credentials.")
    print("=" * 50)
