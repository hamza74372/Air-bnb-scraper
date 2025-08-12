# airbnb_template_scraper.py
import time
import random
import re
import os
import json
from urllib.parse import quote, urlparse, urlunparse
from datetime import datetime

import pandas as pd
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from webdriver_manager.chrome import ChromeDriverManager

# ---------------- CONFIG ----------------
HEADLESS = False
CITIES = [
    "Islamabad", "Lahore", "Karachi", "Rawalpindi", "Multan",
    "Faisalabad", "Hyderabad", "Peshawar", "Quetta", "Sialkot"
]
# Put your example/search URL here. The script will replace the city part of the path.
TEMPLATE_URL = ("https://www.airbnb.com/s/karachi-/homes?refinement_paths%5B%5D=%2Fhomes"
                "&date_picker_type=monthly_stay&monthly_start_date=2025-09-01&monthly_end_date=2026-09-01"
                "&search_type=search_query&flexible_trip_lengths%5B%5D=one_week&monthly_length=3"
                "&price_filter_input_type=1&price_filter_num_nights=365&channel=EXPLORE"
                "&location_bb=QgcllEKSxKxCBcg6QpGTgQ%3D%3D&acp_id=b513a84f-8cc3-4439-bc64-0df14e5810b4"
                "&source=structured_search_input_header")

OUT_CSV = "output/airbnb_by_template_all_cities.csv"
OUT_JSON = "output/airbnb_by_template_all_cities.json"

# Pagination settings
MAX_PAGES_PER_CITY = 20  # Adjust based on how many pages you want to scrape per city
SCROLL_PAUSE_TIME = 3
PAGINATION_WAIT_TIME = 5  # Time to wait after clicking next page
# ----------------------------------------

options = Options()
if HEADLESS:
    options.add_argument("--headless=new")
options.add_argument("--disable-blink-features=AutomationControlled")
options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0 Safari/537.36")
options.add_argument("--start-maximized")
options.add_argument("--disable-web-security")
options.add_argument("--disable-features=VizDisplayCompositor")

driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
driver.set_page_load_timeout(60)

# ---------------- helpers ----------------
def build_city_url_from_template(template_url: str, city: str) -> str:
    """
    Replace the city slug in the template URL path (the third path segment after /s/)
    Example: /s/karachi-/homes -> /s/islamabad-/homes  (keeps trailing hyphen if present in template)
    """
    parsed = urlparse(template_url)
    parts = parsed.path.split('/')  # ['', 's', 'karachi-', 'homes']
    if len(parts) >= 3 and parts[1] == 's':
        orig_third = parts[2]
        # keep hyphen pattern if original endswith '-'
        if orig_third.endswith('-'):
            new_third = quote(city.lower()) + '-'
        else:
            new_third = quote(city.lower())
        parts[2] = new_third
        new_path = '/'.join(parts)
    else:
        # fallback if template path is unexpected
        new_path = f"/s/{quote(city.lower())}/homes"
    new_parsed = parsed._replace(path=new_path)
    return urlunparse(new_parsed)

def accept_cookies_if_present():
    try:
        # Try different cookie acceptance patterns
        cookie_selectors = [
            "//button[contains(text(),'Accept')]",
            "//button[contains(text(),'Agree')]", 
            "//button[contains(text(),'OK')]",
            "//button[contains(@data-testid, 'accept')]",
            "//button[contains(@id, 'cookie')]",
            "//button[contains(text(), 'I agree')]"
        ]
        
        for selector in cookie_selectors:
            try:
                btn = WebDriverWait(driver, 2).until(
                    EC.element_to_be_clickable((By.XPATH, selector))
                )
                if btn and btn.is_displayed():
                    btn.click()
                    time.sleep(1)
                    print("  ✓ Accepted cookies")
                    return
            except TimeoutException:
                continue
    except Exception as e:
        print(f"  ⚠ Cookie handling error: {e}")

CARD_SELECTORS = [
    'div[data-testid="card-container"]',
    'div[itemprop="itemListElement"]',
    'div[data-testid="listing"]',
    '[data-testid="property-card"]',
    'div[role="group"]',
    'a[href*="/rooms/"]'
]

def find_cards(soup):
    for sel in CARD_SELECTORS:
        cards = soup.select(sel)
        if cards:
            print(f"  ✓ Found {len(cards)} cards with selector: {sel}")
            return cards
    return []

def extract_from_card(card):
    text = card.get_text(" ", strip=True)
    # title
    title = ""
    meta = card.select_one('meta[itemprop="name"]')
    if meta and meta.has_attr("content"):
        title = meta["content"].strip()
    else:
        title_selectors = [
            '[data-testid="listing-card-title"]',
            '[data-testid="title"]', 
            '[role="heading"]',
            'h3',
            'h2'
        ]
        for sel in title_selectors:
            t = card.select_one(sel)
            if t:
                title = t.get_text(strip=True)
                break
        
        if not title:
            # fallback
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            title = lines[0] if lines else ""

    # price
    price = ""
    price_selectors = [
        '[data-testid="price"]',
        '[data-testid="price-availability-row"]',
        'span[aria-label*="per night"]',
        'span[aria-label*="total"]'
    ]
    for sel in price_selectors:
        p = card.select_one(sel)
        if p:
            price = p.get_text(" ", strip=True)
            break
    
    if not price:
        m = re.search(r'[\$€£₹]\s*\d[\d,]*', text)
        if m:
            price = m.group(0)

    # rating & reviews
    rating = ""
    reviews = ""
    m = re.search(r'(\d\.\d{1,2})\s*[·•]\s*([\d,]+)\s*reviews?', text, re.IGNORECASE)
    if m:
        rating = m.group(1)
        reviews = m.group(2).replace(",", "")
    else:
        mrev = re.search(r'\(([\d,]+)\)\s*reviews?', text, re.IGNORECASE)
        if mrev:
            reviews = mrev.group(1).replace(",", "")
        mrat = re.search(r'(\d\.\d{1,2})(?!(\d))', text)
        if mrat:
            rating = mrat.group(1)

    # image
    image_url = ""
    img = card.select_one('img')
    if img:
        image_url = img.get("src") or img.get("data-src") or (img.get("srcset") or "").split(" ")[0] or ""

    # listing url
    listing_url = ""
    a = card.select_one('a[href*="/rooms/"]')
    if a and a.has_attr("href"):
        href = a["href"]
        listing_url = href if href.startswith("http") else "https://www.airbnb.com" + href

    return {
        "Title": title,
        "Price": price,
        "Rating": rating,
        "Reviews": reviews,
        "Image_URL": image_url,
        "Listing_URL": listing_url
    }

def click_next_page():
    """Click the next page button (>) to go to the next page"""
    print("  ➡️ Looking for next page button...")
    
    # Multiple selectors for the "next" button
    next_button_selectors = [
        "//a[@aria-label='Next']",
        "//button[@aria-label='Next']",
        "//a[contains(@aria-label, 'Next')]",
        "//button[contains(@aria-label, 'Next')]",
        "//a[text()='>']",
        "//button[text()='>']",
        "//a[contains(text(), '›')]",
        "//button[contains(text(), '›')]",
        "//a[contains(@class, 'next')]",
        "//button[contains(@class, 'next')]",
        "//nav//a[last()]",  # Last pagination link
        "//div[@role='navigation']//a[last()]",
        "//div[contains(@data-testid, 'pagination')]//a[last()]"
    ]
    
    for selector in next_button_selectors:
        try:
            next_btn = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((By.XPATH, selector))
            )
            
            if next_btn and next_btn.is_displayed():
                # Check if button is not disabled
                if (not next_btn.get_attribute("disabled") and 
                    "disabled" not in next_btn.get_attribute("class").lower() and
                    next_btn.get_attribute("aria-disabled") != "true"):
                    
                    # Scroll to button first
                    driver.execute_script("arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});", next_btn)
                    time.sleep(2)
                    
                    # Try clicking with JavaScript first (more reliable)
                    try:
                        driver.execute_script("arguments[0].click();", next_btn)
                        print(f"  ✓ Clicked next page button (JS click)")
                    except:
                        # Fallback to regular click
                        next_btn.click()
                        print(f"  ✓ Clicked next page button (regular click)")
                    
                    # Wait for page to load
                    time.sleep(random.uniform(4, 7))
                    return True
                else:
                    print(f"  ⚠ Next button found but disabled")
                    return False
                    
        except TimeoutException:
            continue
        except Exception as e:
            print(f"  ⚠ Error clicking next button: {e}")
            continue
    
    print("  ⚠ No clickable next page button found")
    return False

def scrape_city_with_pagination(city):
    """Scrape all available pages for a single city using pagination buttons"""
    city_url = build_city_url_from_template(TEMPLATE_URL, city)
    print(f"\n{'='*50}")
    print(f"CITY: {city}")
    print(f"{'='*50}")
    print("Opening:", city_url)
    
    driver.get(city_url)
    accept_cookies_if_present()
    
    # Wait for listings to load
    try:
        WebDriverWait(driver, 20).until(
            EC.any_of(
                EC.presence_of_element_located((By.CSS_SELECTOR, 'div[data-testid="card-container"]')),
                EC.presence_of_element_located((By.CSS_SELECTOR, 'a[href*="/rooms/"]')),
                EC.presence_of_element_located((By.CSS_SELECTOR, 'div[itemprop="itemListElement"]'))
            )
        )
        print("  ✓ Initial listings loaded")
    except TimeoutException as e:
        print(f"  ⚠ Timeout waiting for listings: {e}")
        # Save HTML for debugging
        with open(f"error_{city}.html", "w", encoding="utf-8") as fh:
            fh.write(driver.page_source)
        return []
    
    city_results = []
    seen_in_city = set()
    page_count = 0
    
    while page_count < MAX_PAGES_PER_CITY:
        page_count += 1
        print(f"\n  📄 Processing page {page_count} for {city}...")
        
        # Scroll to make sure all content is loaded on current page
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(2)
        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(1)
        
        # Get current listings
        soup = BeautifulSoup(driver.page_source, "html.parser")
        cards = find_cards(soup)
        
        if not cards:
            print("  ⚠ No cards found on this page")
            break
        
        new_listings_count = 0
        for card in cards:
            entry = extract_from_card(card)
            key = entry.get("Listing_URL") or entry.get("Title")
            
            if key and key not in seen_in_city:
                seen_in_city.add(key)
                entry["City"] = city
                entry["Page"] = page_count
                entry["Scraped_At"] = datetime.utcnow().isoformat()
                city_results.append(entry)
                new_listings_count += 1
        
        print(f"  ✓ Found {len(cards)} cards, {new_listings_count} new listings")
        print(f"  📊 Total unique listings for {city}: {len(city_results)}")
        
        # Check if we've reached the maximum pages or no new listings
        if page_count >= MAX_PAGES_PER_CITY:
            print(f"  🛑 Reached maximum pages limit ({MAX_PAGES_PER_CITY}) for {city}")
            break
            
        if new_listings_count == 0:
            print(f"  🛑 No new listings found on page {page_count}, stopping pagination")
            break
        
        # Try to go to next page
        print(f"  🔄 Attempting to go to page {page_count + 1}...")
        
        # Scroll to bottom to make sure pagination is visible
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(2)
        
        if not click_next_page():
            print("  🛑 No more pages available or next button not found")
            break
        
        # Wait for new page to load and check if URL changed or content changed
        try:
            # Wait for page to load by checking for listings
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, 'div[data-testid="card-container"], a[href*="/rooms/"]'))
            )
            print(f"  ✓ Page {page_count + 1} loaded successfully")
        except TimeoutException:
            print(f"  ⚠ Timeout waiting for page {page_count + 1} to load")
            break
    
    print(f"\n  🎯 Final results for {city}: {len(city_results)} unique listings across {page_count} pages")
    return city_results

# ---------------- main ----------------
results = []

try:
    for city in CITIES:
        city_results = scrape_city_with_pagination(city)
        results.extend(city_results)
        
        # Save intermediate results after each city
        if results:
            df = pd.DataFrame(results)
            df.to_csv(OUT_CSV.replace('.csv', '_temp.csv'), index=False)
            print(f"  💾 Intermediate save: {len(results)} total listings so far")
        
        # Polite pause between cities
        if city != CITIES[-1]:  # Don't sleep after the last city
            sleep_time = random.uniform(8, 15)
            print(f"  😴 Sleeping for {sleep_time:.1f} seconds before next city...")
            time.sleep(sleep_time)

except KeyboardInterrupt:
    print("\n⚠ Interrupted by user — saving what we have...")

finally:
    driver.quit()

# Final save
os.makedirs("output", exist_ok=True)
if results:
    df = pd.DataFrame(results)
    df.to_csv(OUT_CSV, index=False)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    
    # Print summary
    print(f"\n{'='*60}")
    print(f"SCRAPING COMPLETED!")
    print(f"{'='*60}")
    print(f"Total listings scraped: {len(results)}")
    print(f"Files saved:")
    print(f"  📊 CSV: {OUT_CSV}")
    print(f"  📋 JSON: {OUT_JSON}")
    
    # City-wise breakdown
    city_counts = df['City'].value_counts()
    print(f"\nCity-wise breakdown:")
    for city, count in city_counts.items():
        print(f"  {city}: {count} listings")
    
    # Clean up temp file
    temp_file = OUT_CSV.replace('.csv', '_temp.csv')
    if os.path.exists(temp_file):
        os.remove(temp_file)
        
else:
    print("\n❌ No results scraped.")