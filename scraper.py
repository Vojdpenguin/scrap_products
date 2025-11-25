import time
import json
import os
from dotenv import load_dotenv
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from webdriver_manager.chrome import ChromeDriverManager

load_dotenv()

START_URL = os.getenv("START_URL")
HEADLESS = True
DOWNLOAD_IMAGES = True
IMAGES_DIR = "images"
OUT_JSON = "products.json"

THREADS = 12
REQUEST_TIMEOUT = 12
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
HEADERS = {"User-Agent": USER_AGENT}

SCROLL_PAUSE = 1.0
CLICK_WAIT_RETRY = 6
ITEM_WAIT_TIMEOUT = 10
MAX_NO_PROGRESS = 4


def make_driver():
    options = webdriver.ChromeOptions()
    if HEADLESS:
        options.add_argument("--headless=new")

    options.add_argument("--window-size=1920,1080")
    prefs = {
        "profile.managed_default_content_settings.images": 2,
        "profile.managed_default_content_settings.stylesheets": 2,
        "profile.managed_default_content_settings.fonts": 2,
    }
    options.add_experimental_option("prefs", prefs)
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    return webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)


def load_all_products_via_selenium(driver):
    print("\n=== AJAX LOADING PRODUCTS ===")
    no_progress = 0
    prev_count = 0

    while True:
        for _ in range(3):
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(SCROLL_PAUSE)

        try:
            btns = driver.find_elements(By.CSS_SELECTOR, "button.woocommerce-load-more")
            if not btns:
                print("✔ Load-more button not found → finished loading.")
                break
            btn = btns[0]
        except Exception:
            print("✔ Load-more button not found (exception) → finished.")
            break

        try:
            if not btn.is_displayed():
                time.sleep(0.8)
                btns = driver.find_elements(By.CSS_SELECTOR, "button.woocommerce-load-more")
                if not btns or not btns[0].is_displayed():
                    print("✔ Load-more button not visible → finished loading.")
                    break
                btn = btns[0]
        except Exception:
            print("✔ Load-more button not available → finished.")
            break

        try:
            before = len(driver.find_elements(By.CSS_SELECTOR, "div.content-products-list ul li"))
        except Exception:
            before = prev_count

        print(f"Clicking LOAD MORE (current items: {before})")

        clicked_and_increased = False

        for attempt in range(1, CLICK_WAIT_RETRY + 1):
            try:
                btns = driver.find_elements(By.CSS_SELECTOR, "button.woocommerce-load-more")
                if not btns:
                    break
                btn = btns[0]
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
                time.sleep(0.12)
                driver.execute_script("arguments[0].click();", btn)
            except Exception:
                time.sleep(0.5)
                continue

            waited = 0.0
            interval = 0.5
            increased = False
            while waited < ITEM_WAIT_TIMEOUT:
                time.sleep(interval)
                waited += interval
                try:
                    now = len(driver.find_elements(By.CSS_SELECTOR, "div.content-products-list ul li"))
                except Exception:
                    now = before
                if now > before:
                    increased = True
                    prev_count = now
                    print(f"  loaded new items (now {now}) [attempt {attempt}]")
                    break

            if increased:
                clicked_and_increased = True
                break
            else:
                time.sleep(0.6)

        if not clicked_and_increased:
            no_progress += 1
            print(f" No progress after attempts (no_progress={no_progress}).")
            if no_progress >= MAX_NO_PROGRESS:
                print(" Stopping loader due to repeated no-progress.")
                break
            time.sleep(1.0)
        else:
            no_progress = 0
            time.sleep(0.6)

    time.sleep(1.0)
    final_count = len(driver.find_elements(By.CSS_SELECTOR, "div.content-products-list ul li"))
    print(f"Load finished — total products detected in DOM: {final_count}")


def parse_tiles_from_html(page_html, base_url=START_URL):
    soup = BeautifulSoup(page_html, "html.parser")
    items = soup.select("div.content-products-list ul li")
    print(f"\n Total product tiles found in final HTML: {len(items)}")
    products = []

    for item in items:
        a = item.select_one("a[href]")
        if not a:
            continue
        href = a.get("href").strip()
        product_url = urljoin(base_url, href)

        title = None
        ttag = item.select_one("h2.woo-loop-product__title")
        if ttag:
            a2 = ttag.select_one("a")
            title = a2.get_text(strip=True) if a2 else ttag.get_text(strip=True)
        else:
            h = item.select_one("h2, h3")
            if h:
                title = h.get_text(strip=True)

        image = None
        img = item.select_one("img")
        if img:
            for attr in ("data-src", "data-lazy-src", "src", "data-srcset", "srcset"):
                if img.has_attr(attr):
                    val = img.get(attr)
                    if not val:
                        continue
                    if attr in ("srcset", "data-srcset") and "," in val:
                        val = val.split(",")[0].strip().split()[0]
                    image = urljoin(base_url, val.split()[0])
                    break

        products.append({
            "title": title,
            "product_url": product_url,
            "image": image
        })

    return products


def fetch_and_parse_product(prod, session=None):
    url = prod.get("product_url")
    if not url:
        return prod

    s = session or requests.Session()
    headers = {"User-Agent": USER_AGENT}
    try:
        r = s.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        html = r.text
    except Exception as e:
        prod.setdefault("errors", []).append(f"fetch_error:{e}")
        return prod

    soup = BeautifulSoup(html, "html.parser")

    price = None
    discount_price = None
    price_box = soup.select_one(".price")
    if price_box:
        del_tag = price_box.select_one("del")
        ins_tag = price_box.select_one("ins")
        if del_tag and ins_tag:
            price = del_tag.get_text(" ", strip=True)
            discount_price = ins_tag.get_text(" ", strip=True)
        else:
            price = price_box.get_text(" ", strip=True)

    rating = None
    star_strong = soup.select_one(".star-rating strong.rating")
    if star_strong:
        rating = star_strong.get_text(strip=True)
    else:
        star = soup.select_one(".star-rating")
        if star:
            aria = star.get("aria-label") or star.get("title") or star.get_text(" ", strip=True)
            if aria:
                m = re.search(r"(\d+(?:[.,]\d+)?)", aria)
                if m:
                    rating = m.group(1).replace(",", ".")

    additional_info = {}
    table = soup.select_one("#tab-additional_information table.shop_attributes")
    if table:
        for row in table.select("tr"):
            th = row.select_one("th")
            td = row.select_one("td")
            if th and td:
                label = th.get_text(" ", strip=True)
                value = td.get_text(" ", strip=True)
                additional_info[label] = value

    page_image = None
    gallery_img = soup.select_one(".woocommerce-product-gallery img, .woocommerce-main-image img, .product img")
    if gallery_img:
        for attr in ("data-src", "data-lazy-src", "src", "srcset"):
            if gallery_img.has_attr(attr):
                val = gallery_img.get(attr)
                if not val:
                    continue
                if attr in ("srcset", "data-srcset") and "," in val:
                    val = val.split(",")[0].strip().split()[0]
                page_image = urljoin(url, val.split()[0])
                break

    prod["price"] = price
    prod["discount_price"] = discount_price
    prod["rating"] = rating
    prod["additional_info"] = additional_info
    if page_image:
        prod["image"] = page_image

    return prod


def download_image_to_dir(url, dst_dir):
    if not url:
        return None
    os.makedirs(dst_dir, exist_ok=True)
    headers = {"User-Agent": USER_AGENT}
    try:
        r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        fname = os.path.basename(url.split("?")[0]) or f"img_{int(time.time() * 1000)}.jpg"
        fname = re.sub(r"[^\w\-.() ]", "_", fname)
        path = os.path.join(dst_dir, fname)
        with open(path, "wb") as fh:
            fh.write(r.content)
        return path
    except Exception as e:
        print("  [img error]", url, e)
        return None


def main():
    driver = make_driver()
    try:
        driver.get(START_URL)
        time.sleep(1.2)
        load_all_products_via_selenium(driver)

        page_html = driver.page_source
    finally:
        try:
            driver.quit()
        except Exception:
            pass

    products = parse_tiles_from_html(page_html, base_url=START_URL)

    seen = set()
    unique_products = []
    for p in products:
        key = p.get("product_url")
        if key and key not in seen:
            seen.add(key)
            unique_products.append(p)

    print(f"\n=== Will fetch and parse {len(unique_products)} product pages via {THREADS} threads ===")

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    with ThreadPoolExecutor(max_workers=THREADS) as ex:
        futures = [ex.submit(fetch_and_parse_product, p, session) for p in unique_products]
        results = []
        for fut in as_completed(futures):
            try:
                res = fut.result()
            except Exception as e:
                print("Thread error:", e)
                continue
            results.append(res)

    url_to_prod = {p.get("product_url"): p for p in results}
    final_products = [url_to_prod.get(p.get("product_url"), p) for p in unique_products]

    print(f"\nFetched and parsed {len(final_products)} products.")

    if DOWNLOAD_IMAGES:
        print("\n=== DOWNLOADING IMAGES ===")
        with ThreadPoolExecutor(max_workers=8) as ex:
            img_futs = {}
            for p in final_products:
                img_url = p.get("image")
                if img_url:
                    f = ex.submit(download_image_to_dir, img_url, IMAGES_DIR)
                    img_futs[f] = p
                else:
                    p["image_local"] = None
            for f in as_completed(img_futs):
                p = img_futs[f]
                try:
                    path = f.result()
                except Exception:
                    path = None
                p["image_local"] = path

    print(f"\n Saving {len(final_products)} products → {OUT_JSON}")
    with open(OUT_JSON, "w", encoding="utf-8") as fh:
        json.dump(final_products, fh, ensure_ascii=False, indent=2)

    print(" DONE")


if __name__ == "__main__":
    main()
