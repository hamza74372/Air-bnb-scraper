# airbnb_city_scraper.py
import time
import random
import re
import pandas as pd
from urllib.parse import quote
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from bs4 import BeautifulSoup
from datetime import datetime

# ----------------- CONFIG -----------------
HEADLESS = False   # Set True to run headless
CITIES = [
    "Islamabad", "Lahore", "Karachi", "Rawalpindi", "Multan",
    "Faisalabad", "Hyderabad", "Peshawar", "Quetta", "Sialkot",
    "Murree", "Swat", "Hunza", "Gilgit", "Skardu",
    "Abbottabad", "Muzaffarabad", "Gwadar", "Karimabad"
]
  # edit or extend this list
CATEGORIES = ["homes", "apartments", "guesthouses", "villas", "cottages", "bungalows"]
# max pages per category per city (None = no explicit limit)
MAX_PAGES = None
OUT_CSV = "airbnb_scraped.csv"
# ------------------------------------------

options = Options()
if HEADLESS:
    options.add_argument("--headless=new")
options.add_argument("--disable-blink-features=AutomationControlled")
options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0 Safari/537.36")
driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
driver.set_page_load_timeout(60)

# helper: accept cookies if dialog present
def accept_cookies_if_present():
    try:
        btn = WebDriverWait(driver, 3).until(
            EC.element_to_be_clickable((By.XPATH, "//button[contains(text(),'Accept') or contains(text(),'Agree') or contains(text(),'OK')]"))
        )
        if btn and btn.is_displayed():
            btn.click()
            time.sleep(1)
    except Exception:
        pass

def scroll_to_bottom_wait():
    """Scroll to bottom with small pauses to allow lazy loading."""
    last = driver.execute_script("return document.body.scrollHeight")
    for _ in range(3):
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(random.uniform(1.5, 3.0))
        new = driver.execute_script("return document.body.scrollHeight")
        if new == last:
            break
        last = new
    # tiny extra pause
    time.sleep(random.uniform(0.8, 1.5))

# robust card-finding (try several selectors)
CARD_SELECTORS = [
    'div[data-testid="explore-property-card"]',
    'div[data-testid="card-container"]',
    'div[data-testid="property-card"]',
    'div[data-testid="listing-card"]',
    'div[role="group"]',
    'div[itemprop="itemListElement"]'
]

def find_cards(soup):
    for sel in CARD_SELECTORS:
        cards = soup.select(sel)
        if cards:
            return cards
    # fallback: find anchors to /rooms/ and grab their parent container
    anchors = soup.select('a[href*="/rooms/"]')
    parents = []
    for a in anchors:
        parent = a.find_parent()
        if parent and parent not in parents:
            parents.append(parent)
    return parents

def extract_from_card(card):
    """Return dict with extracted fields (best-effort)."""
    text = card.get_text(" ", strip=True)
    # Title extraction - try multiple strategies
    title = None
    # JSON-LD / meta
    meta = card.select_one('meta[itemprop="name"]')
    if meta and meta.has_attr("content"):
        title = meta["content"].strip()
    if not title:
        t = card.select_one('[data-testid="listing-card-title"]')
        if t:
            title = t.get_text(strip=True)
    if not title:
        # try heading/role
        h = card.select_one('[role="heading"]')
        if h:
            title = h.get_text(strip=True)
    if not title:
        # fallback: first non-empty line from card text
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        title = lines[0] if lines else None

    # Location: parse from title (e.g., "Home in Ziarat" -> "Ziarat")
    location = None
    if title and " in " in title:
        # split by " in " and take the last chunk
        location = title.split(" in ", 1)[1].strip()
    else:
        # fallback: look for subtitle spans with possible location
        sub = card.select_one('div[data-testid="listing-card-subtitle"], span[data-testid="subtitle"]')
        if sub:
            s = sub.get_text(" ", strip=True)
            # often "City · rating ..." -> take part before dot or middle dot
            if "·" in s:
                location = s.split("·", 1)[0].strip()
            else:
                location = s.split(" - ", 1)[0].strip()
    if not location:
        location = ""

    # Price: try data-testid price selectors, else regex searching card text
    price = None
    p = card.select_one('[data-testid="price"], [data-testid="price-availability-row"], span[aria-label*="per night"]')
    if p:
        price = p.get_text(" ", strip=True)
    if not price:
        m = re.search(r'[\$€£]\s*\d[\d,]*', text)
        if m:
            price = m.group(0)
    price = price or ""

    # Rating & reviews: try to find patterns: "4.86 · 215 reviews" or "(215)" etc.
    rating = ""
    reviews = ""
    # first try combined pattern
    m = re.search(r'(\d\.\d{1,2})\s*[·•]\s*([\d,]+)\s*reviews?', text, re.IGNORECASE)
    if m:
        rating = m.group(1)
        reviews = m.group(2).replace(",", "")
    else:
        # try parentheses for reviews like "(215)"
        mrev = re.search(r'\(([\d,]+)\)\s*reviews?', text, re.IGNORECASE)
        if mrev:
            reviews = mrev.group(1).replace(",", "")
        # try rating only
        mrat = re.search(r'(\d\.\d{1,2})(?!(\d))', text)
        if mrat:
            rating = mrat.group(1)

    # Image URL: try img src, data-src, srcset
    image_url = ""
    img = card.select_one("img")
    if img:
        image_url = img.get("src") or img.get("data-src") or (img.get("srcset") or "").split(" ")[0] or ""

    # Listing URL: anchor with /rooms/
    listing_url = ""
    a = card.select_one('a[href*="/rooms/"]')
    if a and a.has_attr("href"):
        href = a["href"]
        if href.startswith("http"):
            listing_url = href
        else:
            listing_url = "https://www.airbnb.com" + href

    return {
        "Title": title or "",
        "Location": location or "",
        "Price": price,
        "Rating": rating,
        "Reviews": reviews,
        "Image_URL": image_url,
        "Listing_URL": listing_url
    }

# main scraping loop
results = []
seen_urls = set()

try:
    for city in CITIES:
        city_slug = quote(f"{city}--Pakistan")
        for category in CATEGORIES:
            offset = 0
            page_count = 0
            print(f"\n>>> CITY={city}  CATEGORY={category}")
            while True:
                page_count += 1
                if MAX_PAGES and page_count > MAX_PAGES:
                    print("Reached max pages for this category.")
                    break

                url = f"https://www.airbnb.com/s/{city_slug}/{category}?items_offset={offset}"
                print(f"Loading: {url}  (offset={offset})")
                driver.get(url)
                accept_cookies_if_present()

                # wait for some probable card or fallback element
                try:
                    WebDriverWait(driver, 15).until(
                        EC.any_of(
                            EC.presence_of_element_located((By.CSS_SELECTOR, 'div[data-testid="card-container"]')),
                            EC.presence_of_element_located((By.CSS_SELECTOR, 'div[data-testid="explore-property-card"]')),
                            EC.presence_of_element_located((By.CSS_SELECTOR, 'a[href*="/rooms/"]')),
                        )
                    )
                except Exception:
                    print("  ⚠ nothing found on page (possible block or different layout). Breaking this category.")
                    break

                # scroll to load lazy content
                scroll_to_bottom_wait()

                soup = BeautifulSoup(driver.page_source, "html.parser")
                cards = find_cards(soup)
                n_cards = len(cards)
                print(f"  - cards found on page: {n_cards}")

                if n_cards == 0:
                    print("  No cards found — stopping category.")
                    break

                new_on_page = 0
                for c in cards:
                    entry = extract_from_card(c)
                    urlkey = entry.get("Listing_URL") or entry.get("Title")  # fallback key
                    if not urlkey:
                        continue
                    if urlkey in seen_urls:
                        continue
                    seen_urls.add(urlkey)
                    entry["Category"] = category
                    entry["City"] = city
                    entry["Scraped_At"] = datetime.utcnow().isoformat()
                    results.append(entry)
                    new_on_page += 1

                print(f"  - new listings added from page: {new_on_page}")

                # If no new items were added, likely we've reached the end or duplicate pages — stop
                if new_on_page == 0:
                    print("  No new listings on this page, stopping pagination for this category.")
                    break

                # Pagination using items_offset: increase offset by number of cards found
                offset += n_cards

                # polite pause
                time.sleep(random.uniform(3.5, 7.0))

except KeyboardInterrupt:
    print("Interrupted by user — saving progress...")

finally:
    driver.quit()

# Save results
if results:
    df = pd.DataFrame(results)
    df.to_csv(OUT_CSV, index=False)
    print(f"\nSaved {len(results)} listings to {OUT_CSV}")
else:
    print("\nNo results scraped.")
